"""`Runtime`: one process, one event loop, every component supervised.

Assembles the bus, the hub, the body, the memory, the safety guard, the
behaviors, the tools, the mind and the voice provider from a `RobotConfig`
(plan.md section 4.4). Each long-lived component runs in a named task that
is restarted with exponential backoff and reports on ``component.health``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from asimoov.contracts.audio import PlaybackTracker
from asimoov.contracts.behaviors import BehaviorStatus
from asimoov.contracts.body import Body, BodyContext, BodyManifest
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.face import FaceRenderer, FaceState
from asimoov.contracts.frames import encode_frame
from asimoov.contracts.percepts import Utterance
from asimoov.contracts.tools import ToolResult
from asimoov.contracts.vocab import TOPICS
from asimoov.contracts.voice import VoiceEvents, VoiceProvider
from asimoov.core.behaviors.executor import BehaviorExecutor
from asimoov.core.behaviors.resolver import BehaviorResolver, load_behaviors
from asimoov.core.bus.hub import Hub
from asimoov.core.bus.local import LocalBus
from asimoov.core.clock import Clock, wall_clock
from asimoov.core.config import RobotConfig, Secrets
from asimoov.core.memory.sqlite_store import SqliteMemoryStore
from asimoov.core.mind.initiative import InitiativePolicy
from asimoov.core.mind.injector import Injector
from asimoov.core.mind.mind import Mind
from asimoov.core.mind.prompt import describe_scene
from asimoov.core.plugins import BODY_GROUP, FACE_GROUP, VOICE_GROUP, load_plugin
from asimoov.core.safety import SafetyGuard
from asimoov.core.scene.scene import SceneTracker
from asimoov.core.telemetry import Telemetry
from asimoov.core.tools.builtin import build_builtin_tools
from asimoov.core.tools.registry import ToolRegistry, build_behavior_tools
from asimoov.core.voice_loop import AudioPlan, VoiceLoop, open_audio, plan_audio
from asimoov.voice.session import SessionManager

log = logging.getLogger(__name__)

RESTART_MIN_S = 0.5
RESTART_MAX_S = 30.0
RESTART_RESET_S = 60.0
VOICE_EVENT_TOPIC = TOPICS.VOICE_EVENT


@dataclass
class ComponentHealth:
    """What `Supervisor` publishes about one component."""

    name: str
    running: bool = True
    restarts: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.name,
            "running": self.running,
            "restarts": self.restarts,
            "last_error": self.last_error,
        }


class Supervisor:
    """Runs named tasks, restarting them with exponential backoff."""

    def __init__(self, bus=None) -> None:
        self.bus = bus
        self.components: dict[str, ComponentHealth] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def spawn(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        """Run ``factory()`` forever under the name ``name``."""
        self.components[name] = ComponentHealth(name=name)
        self._tasks[name] = asyncio.create_task(self._supervise(name, factory), name=name)

    async def stop_all(self) -> None:
        for name, task in list(self._tasks.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._tasks.pop(name, None)
            health = self.components.get(name)
            if health is not None:
                health.running = False

    async def _supervise(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        delay = RESTART_MIN_S
        while True:
            started = time.monotonic()
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                health = self.components[name]
                health.restarts += 1
                health.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("component %s crashed (restart %d)", name, health.restarts)
                await self._publish(health)
                if time.monotonic() - started > RESTART_RESET_S:
                    delay = RESTART_MIN_S
                await asyncio.sleep(delay)
                delay = min(delay * 2, RESTART_MAX_S)

    async def _publish(self, health: ComponentHealth) -> None:
        if self.bus is not None:
            await self.bus.publish(
                TOPICS.COMPONENT_HEALTH_PATTERN, health.to_dict(), kind="state"
            )


RENEWAL_METHODS = ("request_summary", "wait_ready", "prune_items")


def _is_renewable(provider: Any) -> bool:
    """True if ``provider`` implements `voice.session.RenewableProvider`."""
    return all(callable(getattr(provider, name, None)) for name in RENEWAL_METHODS)


def openai_api_key() -> str | None:
    """The OpenAI key, from the one place `doctor`, `run` and the provider read.

    ``$ASIMOOV_OPENAI_API_KEY``, then ``~/.asimoov/secrets.yaml``, then
    ``$OPENAI_API_KEY`` as a last resort. Never logged, never printed.
    """
    return Secrets().get("openai_api_key")


def _voice_factory(provider_cls: type) -> Callable[[], VoiceProvider]:
    """Build providers of ``provider_cls``, handing them the resolved key.

    A provider whose constructor takes no ``api_key`` (the fake, a local
    pipeline) is built untouched; the key is never a positional surprise.
    """
    takes_key = "api_key" in inspect.signature(provider_cls).parameters

    def factory() -> VoiceProvider:
        if not takes_key:
            return provider_cls()
        return provider_cls(api_key=openai_api_key())

    return factory


class _VoiceBridge(VoiceEvents):
    """Turns `VoiceProvider` callbacks into bus envelopes (and nothing else).

    Everything the core reacts to goes through the bus, which is exactly
    why a recorded session can be replayed: the mind cannot tell a live
    provider from a replay file.
    """

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        # Strong references: a task only referenced by the event loop can be
        # garbage-collected mid-flight (review, major 15).
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coroutine) -> None:
        loop = self.runtime.loop
        if loop is None or loop.is_closed():
            log.warning("voice event dropped: the runtime loop is gone")
            coroutine.close()
            return
        loop.call_soon_threadsafe(self._create_task, loop, coroutine)

    def _create_task(self, loop: asyncio.AbstractEventLoop, coroutine) -> None:
        task = loop.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def on_speech_started(self) -> None:
        self._spawn(
            self.runtime.bus.publish(
                TOPICS.PERCEPT_PREFIX + "speech_started",
                {"type": "speech_started", "source": "voice"},
            )
        )

    def on_speech_ended(self) -> None:
        self.runtime.begin_turn()
        self._spawn(
            self.runtime.bus.publish(
                TOPICS.PERCEPT_PREFIX + "speech_ended",
                {"type": "speech_ended", "source": "voice"},
            )
        )

    def on_utterance(self, utterance: Utterance) -> None:
        self._spawn(
            self.runtime.bus.publish(
                TOPICS.PERCEPT_PREFIX + "utterance", utterance.to_dict()
            )
        )

    def on_tool_call(self, name: str, params: dict[str, Any], call_id: str) -> None:
        self._spawn(
            self.runtime.bus.publish(
                VOICE_EVENT_TOPIC,
                {"type": "tool_call", "name": name, "params": params, "call_id": call_id},
                kind="cmd",
            )
        )

    def on_audio_out(self, item_id: str, pcm16: bytes) -> None:
        self.runtime.mark_first_audio()
        self._spawn(self.runtime.play_audio(item_id, pcm16))

    def on_response_started(self, response_id: str) -> None:
        # v1.3. Most responses are started by the model, not by us; without
        # this the injector believes the robot is silent and talks over it.
        self.runtime.mind.set_response_active(True)
        self._spawn(
            self.runtime.bus.publish(
                VOICE_EVENT_TOPIC,
                {"type": "response_started", "response_id": response_id},
                kind="reply",
            )
        )

    def on_response_done(self, response_id: str) -> None:
        self._spawn(
            self.runtime.bus.publish(
                VOICE_EVENT_TOPIC,
                {"type": "response_done", "response_id": response_id},
                kind="reply",
            )
        )

    def on_assistant_text(self, text: str) -> None:
        self._spawn(self.runtime.record_assistant_text(text))


@dataclass
class Runtime:
    """A whole robot, assembled and supervised."""

    config: RobotConfig
    body: Body
    voice: VoiceProvider | None = None
    faces: tuple[FaceRenderer, ...] = ()
    memory: Any = None
    telemetry: Telemetry | None = None
    clock: Clock = wall_clock
    tick_s: float = 1.0
    hub_enabled: bool = True
    fragments: tuple[str, ...] = ()
    voice_factory: Callable[[], VoiceProvider] | None = None
    audio_enabled: bool = True

    bus: LocalBus = field(default_factory=lambda: LocalBus(src="core"))
    hub: Hub | None = None
    loop: asyncio.AbstractEventLoop | None = None
    started: bool = False
    sessions: SessionManager | None = None
    voice_loop: VoiceLoop | None = None
    audio_plan: AudioPlan | None = None

    def __post_init__(self) -> None:
        self.supervisor = Supervisor(self.bus)
        self.tracker = SceneTracker()
        self.registry = ToolRegistry(telemetry=self.telemetry)
        self.behaviors = load_behaviors(self.config.behaviors_dir)
        self._bind_body_manifest()
        self.injector = Injector(self._deliver_injection, bus=self.bus, clock=self.clock)
        self.mind = Mind(
            bus=self.bus,
            persona=self.config.persona,
            body_manifest=self.body.manifest,
            injector=self.injector,
            tracker=self.tracker,
            initiative=InitiativePolicy(
                self.config.persona.initiative,
                language=self.config.persona.language,
                clock=self.clock,
            ),
            memory=self.memory,
            request_response=self._request_response,
            clock=self.clock,
            tick_s=self.tick_s,
            fragments=self.fragments,
        )
        self._turn = None
        self._first_audio_marked = False
        self._tracker_pushes_energy = False
        self._warned_missing_tool_output = False
        self._subscriptions: list[Any] = []
        self._tasks: set[asyncio.Task] = set()
        self._first_voice: VoiceProvider | None = None
        self._episode: Any = None
        self._sink: Any = None
        self._known_person_ids: set[str] = set()

    def _bind_body_manifest(self) -> None:
        """Build everything derived from the body manifest: safety, behaviors, tools.

        Called again right after `Body.start`, because a body may only learn
        what it is once its link is up: the InMoov bust reads its channel
        table from the firmware, so before `start` it declares no capability
        at all and the resolver would hide every gesture it can do.
        """
        self._bound_manifest = self.body.manifest
        self.safety = SafetyGuard(
            self.body, stop_phrases=self.config.stop_phrases, bus=self.bus
        )
        self.resolver = BehaviorResolver.from_body(self.behaviors, self._bound_manifest)
        self.executor = BehaviorExecutor(
            self.body,
            self.resolver,
            telemetry=self.telemetry,
            on_completion=self._on_action_done,
            max_continuous_motion_s=self.safety.max_continuous_motion_s,
        )
        self.safety.on_stop = self.executor.cancel_all
        mind = getattr(self, "mind", None)
        if mind is not None:
            mind.body_manifest = self._bound_manifest

    # -- construction -----------------------------------------------------

    @classmethod
    def build(
        cls,
        config: RobotConfig,
        *,
        body_name: str | None = None,
        voice_name: str | None = None,
        face_names: tuple[str, ...] | None = None,
        memory=None,
        telemetry: Telemetry | None = None,
        clock: Clock = wall_clock,
        tick_s: float = 1.0,
        hub_enabled: bool = True,
    ) -> Runtime:
        """Resolve plugins by name and assemble a runtime from ``config``."""
        body_cls = load_plugin(BODY_GROUP, body_name or config.body.type)
        body = body_cls()
        voice: VoiceProvider | None = None
        voice_factory: Callable[[], VoiceProvider] | None = None
        if voice_name != "none":
            provider_name = voice_name or (
                config.persona.voice.provider if config.persona.voice else "fake"
            )
            voice_factory = _voice_factory(load_plugin(VOICE_GROUP, provider_name))
            voice = voice_factory()
        names = config.faces if face_names is None else face_names
        faces = tuple(load_plugin(FACE_GROUP, name)() for name in names if name != "none")
        store = memory if memory is not None else SqliteMemoryStore(config.memory_file())
        return cls(
            config=config,
            body=body,
            voice=voice,
            faces=faces,
            memory=store,
            telemetry=telemetry,
            clock=clock,
            tick_s=tick_s,
            hub_enabled=hub_enabled and config.hub.enabled,
            voice_factory=voice_factory,
        )

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        if self.memory is not None and hasattr(self.memory, "open"):
            await self.memory.open()
        self.mind.memory = self.memory

        await self.bus.start()
        if self.hub_enabled:
            self.hub = Hub(self.bus, host=self.config.hub.host, port=self.config.hub.port)
            await self.hub.start()

        await self.body.start(
            BodyContext(
                config=self.config.body.config,
                publish=self.bus.publish,
                subscribe=self.bus.subscribe,
                request=self.bus.request,
                bus=self.bus,
            )
        )

        if self.body.manifest is not self._bound_manifest:
            log.info("body %s declared its manifest at connect", self.body.manifest.name)
            self._bind_body_manifest()

        self._register_tools()
        self._subscriptions = [
            self.bus.subscribe(TOPICS.PERCEPT_PREFIX + "utterance", self._on_utterance),
            self.bus.subscribe(VOICE_EVENT_TOPIC, self._on_voice_event),
            self.bus.subscribe(TOPICS.FACE_STATE, self._on_face_state),
            self.bus.subscribe(TOPICS.BODY_REPLY, self._on_body_reply),
            self.bus.subscribe(TOPICS.BODY_MANIFEST, self._on_body_manifest),
            self.bus.subscribe(TOPICS.SAFETY_ESTOP, self._on_estop_request),
        ]

        for face in self.faces:
            await face.start(self._face_context())
            self._mount_face(face)

        await self.safety.start(self.supervisor.spawn)
        await self.mind.start(self.supervisor.spawn)

        if self.voice is not None:
            await self.mind.refresh_memories()
            await self._start_voice()
        self._wire_playback_tracker()
        self.started = True
        log.info("runtime started (body=%s)", self.body.manifest.name)

    async def _start_voice(self) -> None:
        """Open the devices, wire barge-in and session renewal, connect.

        `voice/` has always had the pieces; until this method nothing in the
        process ever opened a microphone (review, blocker 2 and major 14).
        """
        assert self.voice is not None
        bridge = _VoiceBridge(self)
        self._first_voice = self.voice
        rate = type(self.voice).sample_rate_hz

        if self.audio_enabled:
            self.audio_plan = plan_audio(self.config.audio, self.body)
            if self.audio_plan.ready:
                source, sink = open_audio(
                    self.audio_plan, self.config.audio, self.body, sample_rate_hz=rate
                )
                self.voice_loop = VoiceLoop(
                    self.voice,
                    source,
                    sink,
                    on_barge_in=self._on_barge_in,
                )
            else:
                log.warning("no audio device: %s", self.audio_plan.reason)

        if self.voice_factory is not None and _is_renewable(self.voice):
            self.sessions = SessionManager(
                self._next_voice,
                self._voice_config(),
                bridge,
                is_silent=self._is_silent,
                context_provider=self._scene_context,
                on_summary=self._on_session_summary,
                on_renewal=self._on_session_renewed,
            )
            await self.sessions.start()
            self.supervisor.spawn("voice.session", self._session_forever)
        else:
            await self.voice.start(bridge, self._voice_config())

        if self.voice_loop is not None:
            await self.voice_loop.start()

    def _next_voice(self) -> VoiceProvider:
        """The provider `SessionManager` starts: the built one, then fresh ones."""
        first, self._first_voice = self._first_voice, None
        if first is not None:
            return first
        assert self.voice_factory is not None
        return self.voice_factory()

    def _is_silent(self) -> bool:
        if self.voice_loop is not None:
            return self.voice_loop.is_silent()
        return not self.injector.response_active

    def _scene_context(self) -> str:
        return describe_scene(self.mind.scene)

    def _on_barge_in(self, item_id: str | None) -> None:
        if self.telemetry is not None:
            self.telemetry.counter("barge_in_count")

    def _on_session_summary(self, summary: str) -> None:
        """A renewal produced a summary: it is the episode of what just happened."""
        self._spawn(self._close_episode(summary, "renewal"))

    def _on_session_renewed(self, reason: str) -> None:
        provider = self.sessions.active if self.sessions is not None else None
        if provider is None:
            return
        self.voice = provider  # type: ignore[assignment]
        if self.voice_loop is not None:
            self.voice_loop.set_provider(provider)
        self.mind.transcript.clear()
        self._spawn(self._refresh_after_renewal())
        if self.telemetry is not None:
            self.telemetry.counter("session_renewals")

    async def _refresh_after_renewal(self) -> None:
        await self.mind.refresh_memories()

    async def _session_forever(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            if self.sessions is not None:
                await self.sessions.tick()

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def open_episode(self) -> None:
        """Open an episode the first time anything is said.

        Opened lazily so a robot that nobody talked to leaves no empty row,
        and closed with a real summary at renewal and at shutdown (review,
        medium: "episodes never written").
        """
        if self.memory is None or self._episode is not None:
            return
        participants = tuple(
            person.person_id for person in self.mind.scene.known_people() if person.person_id
        )
        self._episode = await self.memory.start_episode(participants)

    async def _close_episode(self, summary: str, reason: str) -> None:
        episode, self._episode = self._episode, None
        summary = summary.strip()
        if self.memory is None or episode is None or not summary:
            self._episode = episode
            return
        await self.memory.end_episode(episode.id, summary)
        log.info("episode %s closed (%s)", episode.id[:8], reason)

    async def stop(self) -> None:
        self.started = False
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions = []
        await self.supervisor.stop_all()
        if self.voice_loop is not None:
            await self.voice_loop.stop()
            self.voice_loop = None
        await self._close_episode(" ".join(self.mind.transcript), "session end")
        if self.sessions is not None:
            await self.sessions.stop()
            self.sessions = None
        elif self.voice is not None:
            await self.voice.stop()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        await self.mind.stop()
        await self.safety.stop()
        await self.executor.cancel_all("shutdown")
        for face in self.faces:
            await face.stop()
        await self.body.stop()
        if self.hub is not None:
            await self.hub.stop()
            self.hub = None
        await self.bus.stop()
        if self.memory is not None and hasattr(self.memory, "close"):
            await self.memory.close()
        if self.telemetry is not None:
            self.telemetry.close()

    async def run_forever(self) -> None:
        """Block until cancelled (Ctrl+C is handled by the CLI)."""
        await asyncio.Event().wait()

    # -- faces ------------------------------------------------------------

    def _face_context(self) -> dict[str, Any]:
        """The ctx every `FaceRenderer.start` receives."""
        return {
            "bus": self.bus,
            "host": self.config.hub.host,
            "port": self.hub.bound_port if self.hub is not None else self.config.hub.port,
            "token": self.hub.token if self.hub is not None else None,
            "on_estop": self._on_face_estop,
            # The face page's webcam is a camera like any other: without this
            # its frames were decoded and thrown away (review, blocker 6).
            "on_frame": self._on_face_frame,
            "clock": self.clock,
        }

    async def _on_face_frame(
        self, topic: str, ts_ms: int, content_type: str, jpeg: bytes
    ) -> None:
        """Put a browser camera frame on the bus, where perception reads it."""
        await self.bus.publish_frame(encode_frame(topic, jpeg, ts_ms=int(ts_ms)))

    def _mount_face(self, face: FaceRenderer) -> None:
        """Mount a renderer's optional HTTP hook and WebSocket path on the hub.

        A renderer that serves a page (WS6's `WebFace`) exposes
        ``process_request`` (a `contracts.face.ProcessRequestHook`) and/or
        ``ws_path`` plus ``attach(connection, path=...)``. Renderers without
        them (Kivy, servos) need no hub at all.
        """
        if self.hub is None:
            return
        hook = getattr(face, "process_request", None)
        if hook is not None:
            self.hub.set_process_request_hook(hook)
        ws_path = getattr(face, "ws_path", None)
        attach = getattr(face, "attach", None)
        if ws_path and attach is not None:
            self.hub.route(ws_path, attach)

    async def _on_face_estop(self, reason: str = "face_button") -> None:
        await self.safety.stop_all(reason)

    async def _on_estop_request(self, envelope: Envelope) -> None:
        """Anyone on the bus can ask for the e-stop on `TOPICS.SAFETY_ESTOP`."""
        await self.safety.stop_all(str(envelope.data.get("reason") or "safety.estop"))

    # -- tools ------------------------------------------------------------

    def _register_tools(self) -> None:
        # Rebuilt from scratch: called again whenever the body republishes
        # its manifest, and a gesture it lost must stop being advertised.
        self.registry.clear()
        self.registry.register_all(
            build_behavior_tools(
                self.resolver,
                self.executor,
                emotions=self.config.persona.emotions,
                scene_provider=lambda: self.mind.scene,
            )
        )
        self.registry.register_all(
            build_builtin_tools(
                scene_provider=lambda: self.mind.scene,
                memory=self.memory,
                mind=self.mind,
                safety=self.safety,
                bus=self.bus,
                clock=self.clock,
            ),
            replace=True,
        )
        expression = self.registry.get("set_expression")
        if expression is not None:
            # The emotion belongs to the mind, which publishes it on
            # `face.state` for every renderer *and* for the body; running
            # the `express` behavior on top would set it in one place only.
            self.registry.register(expression.spec, self._set_expression, replace=True)

    async def _set_expression(self, params: dict[str, Any]) -> ToolResult:
        emotion = str(params.get("emotion") or "")
        try:
            self.mind.set_emotion(emotion, float(params.get("intensity", 1.0)))
        except ValueError as exc:
            return ToolResult(status=BehaviorStatus.ERROR, reason=str(exc))
        return ToolResult(status=BehaviorStatus.OK, content={"emotion": emotion})

    def _voice_config(self) -> dict[str, Any]:
        voice = self.config.persona.voice
        return {
            "provider": voice.provider if voice else "fake",
            "model": voice.model if voice else "",
            "voice": voice.voice if voice else "",
            "transcription_language": (
                voice.transcription_language if voice else self.config.persona.language
            ),
            "instructions": self.mind.system_prompt(),
            # `ToolSpec` objects, not dicts: the provider owns the wire
            # format and rejects anything else (review, blocker 1).
            "tools": tuple(self.registry.specs()),
        }

    # -- bus handlers -----------------------------------------------------

    async def _on_utterance(self, envelope: Envelope) -> None:
        text = str(envelope.data.get("text") or "")
        if not envelope.data.get("final"):
            return
        await self.open_episode()
        if await self.safety.check_utterance(text):
            log.warning("emergency stop phrase detected in an utterance")

    async def _on_voice_event(self, envelope: Envelope) -> None:
        event = envelope.data.get("type")
        if event == "response_done":
            await self.on_response_done(str(envelope.data.get("response_id") or ""))
            return
        if event != "tool_call":
            return
        name = str(envelope.data.get("name") or "")
        params = envelope.data.get("params") or {}
        call_id = str(envelope.data.get("call_id") or "")
        result = await self.registry.call(name, params)
        await self.bus.publish(
            VOICE_EVENT_TOPIC,
            {
                "type": "tool_result",
                "name": name,
                "call_id": call_id,
                "result": result.to_dict(),
            },
            kind="reply",
            corr=envelope.id,
        )
        await self._send_tool_result(call_id, result)

    async def _on_body_manifest(self, envelope: Envelope) -> None:
        """A body says what it is, whenever its link comes up (review, major 11).

        `Body.start` returns before the link is up, so a bust that reads its
        channel table from the firmware declares no capability at all for the
        first few hundred milliseconds and the resolver hides every gesture
        it can do. Rebinding here, and again on a reconnect, is the fix.
        """
        manifest = BodyManifest.from_dict(dict(envelope.data))
        if manifest == self._bound_manifest:
            return
        log.info("body %s republished its manifest, rebinding", manifest.name)
        self.body.manifest = manifest
        self._bind_body_manifest()
        self._register_tools()
        await self._refresh_voice_tools()

    async def _refresh_voice_tools(self) -> None:
        """Tell the live session which tools exist now."""
        specs = tuple(self.registry.specs())
        if self.sessions is not None:
            self.sessions.update_config(self._voice_config())
        update = getattr(self.voice, "update_tools", None)
        if update is not None:
            await update(specs)

    async def _on_face_state(self, envelope: Envelope) -> None:
        state = FaceState.from_dict(envelope.data)
        for face in self.faces:
            await face.render(state)
        if "face.screen" in self.body.manifest.capabilities:
            await self.body.set_face(state)

    # -- voice ------------------------------------------------------------

    async def _send_tool_result(self, call_id: str, result: ToolResult) -> None:
        if self.voice is None:
            return
        provider = type(self.voice)
        if provider.send_tool_result is VoiceProvider.send_tool_result:
            # The v1.2 default is a documented no-op; say so once rather than
            # let tool results vanish between the bus and the model.
            if not self._warned_missing_tool_output:
                self._warned_missing_tool_output = True
                log.warning(
                    "voice provider %s does not implement send_tool_result(): tool results "
                    "stay on the bus and never reach the model",
                    provider.__name__,
                )
            return
        # The whole `ToolResult`, per `contracts.voice.VoiceProvider`: the
        # provider decides how to serialize it (Neon sent a stringified
        # "ok"). The provider is also the single owner of the follow-up
        # `response.create`; asking for one here too sent two (review,
        # blocker 3).
        await self.voice.send_tool_result(call_id, result)

    async def _deliver_injection(self, text: str) -> None:
        if self.voice is None:
            return
        await self.voice.inject_system_text(text)

    async def _request_response(self, instructions: str | None) -> None:
        if self.injector.response_active:
            return
        self.mind.set_response_active(True)
        await self.bus.publish(
            VOICE_EVENT_TOPIC,
            {"type": "response_requested", "instructions": instructions},
            kind="cmd",
        )
        if self.voice is not None:
            await self.voice.request_response(instructions)

    async def on_response_done(self, response_id: str) -> None:
        self.mind.set_response_active(False)
        self._first_audio_marked = False
        await self.injector.pump()

    async def record_assistant_text(self, text: str) -> None:
        """Journal what the robot itself said, and keep it in the transcript.

        The provider hands us the assistant transcript of a finished response
        (`VoiceEvents.on_assistant_text`), so the episode summary covers both
        sides of the conversation without transcribing our own audio.
        """
        text = text.strip()
        if not text:
            return
        await self.open_episode()
        self.mind.transcript.append(text)
        await self.bus.publish(
            VOICE_EVENT_TOPIC, {"type": "assistant_text", "text": text}, kind="reply"
        )
        if self.memory is not None:
            await self.memory.journal(text, kind="said")

    def _wire_playback_tracker(self) -> None:
        """Let the speaker push the audible head instead of being polled.

        `contracts.audio.PlaybackTracker` exposes optional hooks (v1.2): a
        tracker backed by a real device knows when its head moved, so `lip`
        follows what is coming out of the speaker rather than what was last
        written. Trackers without the hooks keep the polling in `play_audio`.
        """
        self._sink = self.body.audio_sink()
        if self._sink is None and self.voice_loop is not None:
            self._sink = self.voice_loop.sink
        tracker = self._sink.tracker() if self._sink is not None else None
        if tracker is None:
            return
        loop = self.loop
        tracker.set_loop(loop)
        tracker.set_energy_callback(self.mind.set_lip, loop)
        tracker.set_timestamp_callback(self._on_playback_timestamp, loop)
        self._tracker_pushes_energy = (
            type(tracker).set_energy_callback is not PlaybackTracker.set_energy_callback
        )

    def _on_playback_timestamp(self, name: str, unix_s: float) -> None:
        """A playback milestone (``first_audio_played_ts``) joins the turn span."""
        if self._turn is not None:
            self._turn.mark(name.removesuffix("_ts"))

    async def play_audio(self, item_id: str, pcm16: bytes) -> None:
        sink = self._sink if self._sink is not None else self.body.audio_sink()
        if sink is None:
            return
        await sink.play(item_id, pcm16)
        if self._tracker_pushes_energy:
            return
        tracker = sink.tracker()
        if tracker is not None:
            self.mind.set_lip(tracker.energy_at_head())

    def begin_turn(self) -> None:
        if self.telemetry is not None:
            self._turn = self.telemetry.turn()
            self._first_audio_marked = False

    def mark_first_audio(self) -> None:
        if self._turn is not None and not self._first_audio_marked:
            self._first_audio_marked = True
            self._turn.mark("first_audio_delta")

    # -- executor feedback ------------------------------------------------

    async def _on_action_done(self, action_id: str, name: str, status: str) -> None:
        """A long behavior the core drives finished: announce it like a body does."""
        await self.bus.publish(
            TOPICS.BODY_REPLY,
            {"action_id": action_id, "name": name, "status": status, "reason": None},
            kind="reply",
        )

    async def _on_body_reply(self, envelope: Envelope) -> None:
        """Tell the model a long action ended, whoever ran it.

        Every producer of a long action -- the core's `BehaviorExecutor`, the
        Go2, the InMoov bust -- publishes the same
        ``{action_id, name, status, reason}`` here, so there is one path from
        "the movement is over" to "the model knows".
        """
        action_id = str(envelope.data.get("action_id") or "")
        if not action_id:
            return  # a `stop_all` acknowledgement, not an action outcome
        name = str(envelope.data.get("name") or "action")
        status = str(envelope.data.get("status") or "done")
        reason = envelope.data.get("reason")
        note = f"[Action {action_id[:6]} {name}: {status}"
        note += f" ({reason})]" if reason else "]"
        await self.mind.inject(note)

    def health(self) -> dict[str, Any]:
        """Component health, as `asimoov doctor` prints it."""
        return {
            name: health.to_dict() for name, health in self.supervisor.components.items()
        }
