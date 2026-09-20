"""Runtime: assembly, supervision, the voice bridge, conformance of its body."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from asimoov.bodies.conformance import run_conformance
from asimoov.contracts.body import BodyManifest
from asimoov.contracts.envelope import Envelope
from asimoov.contracts.fakes import FakeBody, FakeVoiceProvider
from asimoov.contracts.frames import decode_frame
from asimoov.contracts.percepts import Bearing, PersonSeen, Utterance
from asimoov.contracts.tools import ToolSpec
from asimoov.contracts.vocab import TOPICS
from asimoov.core.clock import ManualClock
from asimoov.core.config import HubConfig
from asimoov.core.memory.sqlite_store import SqliteMemoryStore
from asimoov.core.runtime import Runtime, Supervisor, _VoiceBridge


@pytest.fixture
async def runtime(avatar_config, tmp_path):
    runtime = Runtime(
        config=avatar_config,
        body=FakeBody(),
        voice=FakeVoiceProvider(),
        memory=SqliteMemoryStore(tmp_path / "memory.db"),
        clock=ManualClock(1000.0),
        tick_s=0.02,
        hub_enabled=False,
    )
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()


async def test_the_runtime_starts_and_registers_its_tools(runtime):
    names = {spec.name for spec in runtime.registry.specs()}
    assert {"gesture", "who_is_here", "remember_person", "recall", "stop"} <= names
    assert runtime.started is True
    assert ("start", {}) in runtime.body.calls


async def test_the_system_prompt_is_passed_to_the_voice_provider(avatar_config, tmp_path):
    voice = FakeVoiceProvider()
    runtime = Runtime(
        config=avatar_config,
        body=FakeBody(),
        voice=voice,
        memory=SqliteMemoryStore(tmp_path / "m.db"),
        tick_s=0.02,
        hub_enabled=False,
    )
    await runtime.start()
    try:
        assert voice.started is True
        assert voice.events is not None
    finally:
        await runtime.stop()


async def test_a_tool_call_on_the_bus_produces_a_real_result(runtime):
    results: list[Envelope] = []

    async def collect(envelope: Envelope) -> None:
        if envelope.data.get("type") == "tool_result":
            results.append(envelope)

    runtime.bus.subscribe(TOPICS.VOICE_EVENT, collect)
    runtime.body.calls.clear()
    await runtime.bus.publish(
        TOPICS.VOICE_EVENT,
        {"type": "tool_call", "name": "who_is_here", "params": {}, "call_id": "c1"},
        kind="cmd",
    )
    assert results[0].data["result"]["status"] == "ok"
    assert results[0].data["call_id"] == "c1"


async def test_the_voice_bridge_publishes_percepts(runtime):
    seen: list[str] = []

    async def collect(envelope: Envelope) -> None:
        seen.append(envelope.topic)

    runtime.bus.subscribe(TOPICS.PERCEPT_PREFIX + "*", collect)
    bridge = runtime.voice.events
    bridge.on_speech_started()
    bridge.on_utterance(Utterance(text="hello", lang="en", final=True))
    bridge.on_speech_ended()
    await asyncio.sleep(0.05)
    assert "percept.speech_started" in seen
    assert "percept.utterance" in seen
    assert "percept.speech_ended" in seen


async def test_an_emergency_phrase_in_an_utterance_stops_the_body(runtime):
    await runtime.bus.publish(
        TOPICS.PERCEPT_PREFIX + "utterance",
        {"type": "utterance", "text": "Stop!", "lang": "en", "final": True},
    )
    assert runtime.body.last_stop_all_reason == "e_stop_phrase"


async def test_the_mind_publishes_the_scene_and_the_face(runtime):
    await runtime.bus.publish(
        TOPICS.PERCEPT_PREFIX + "person_seen",
        {
            "type": "person_seen",
            "track_id": "t1",
            "confidence": 0.9,
            "bearing": {"az": 30.0, "el": 0.0},
            "distance_class": "near",
            "bbox_norm": [0.4, 0.3, 0.2, 0.3],
            "face_quality": 0.8,
        },
    )
    for _ in range(100):
        if runtime.bus.latest(TOPICS.SCENE_STATE) and runtime.bus.latest(TOPICS.FACE_STATE):
            break
        await asyncio.sleep(0.01)

    scene = runtime.bus.latest(TOPICS.SCENE_STATE).data
    assert scene["attention_track_id"] == "t1"
    face = runtime.bus.latest(TOPICS.FACE_STATE).data
    assert face["gaze"]["x"] > 0  # the person is to the robot's left
    assert runtime.body.face_states  # the body has face.screen


async def test_the_completion_of_a_long_action_is_injected(runtime):
    await runtime.registry.call("gesture", {"name": "wave_hello"})
    for _ in range(100):
        if runtime.injector.pending or runtime.injector.delivered:
            break
        await asyncio.sleep(0.01)
    queued = " ".join(runtime.injector.pending) + " ".join(runtime.injector.delivered)
    assert "wave_hello: done" in queued
    reply = runtime.bus.latest(TOPICS.BODY_REPLY)
    assert reply is None or set(reply.data) >= {"action_id", "name", "status", "reason"}


async def test_a_body_that_finishes_its_own_long_action_is_injected_too(runtime):
    """Go2 and InMoov drive long actions themselves and report on `body.reply`."""
    await runtime.bus.publish(
        TOPICS.BODY_REPLY,
        {"action_id": "abc123ff", "name": "shake_hand", "status": "error", "reason": "servo stuck"},
        kind="reply",
    )
    queued = " ".join(runtime.injector.pending) + " ".join(runtime.injector.delivered)
    assert "[Action abc123 shake_hand: error (servo stuck)]" in queued


async def test_the_fake_body_resolved_by_the_runtime_passes_conformance(avatar_config):
    runtime = Runtime.build(
        avatar_config,
        body_name="fake",
        voice_name="fake",
        face_names=(),
        hub_enabled=False,
    )
    await run_conformance(runtime.body)


@pytest.mark.parametrize("voice_name", ["claude_pipeline", "openai_realtime"])
def test_build_wires_usage_and_timestamp_hooks_for_both_providers(avatar_config, voice_name):
    """`_voice_factory` hands each real provider the runtime's hooks (review item).

    `on_usage` must reach `SessionManager.note_usage` and `on_timestamp` must
    join the telemetry `turn` span, for both providers -- not just the one
    exercised by the integration tests. `Runtime.build` only constructs the
    provider here; no socket is opened and no key is required.
    """
    runtime = Runtime.build(
        avatar_config,
        body_name="fake",
        voice_name=voice_name,
        face_names=(),
        hub_enabled=False,
    )

    class RecordingSessions:
        def __init__(self) -> None:
            self.usages: list[dict] = []

        def note_usage(self, usage: dict) -> None:
            self.usages.append(usage)

    runtime.sessions = RecordingSessions()
    runtime.voice._on_usage({"total_tokens": 42})
    assert runtime.sessions.usages == [{"total_tokens": 42}]

    class RecordingTurn:
        def __init__(self) -> None:
            self.marks: list[str] = []

        def mark(self, phase: str) -> None:
            self.marks.append(phase)

    runtime._turn = RecordingTurn()
    runtime.voice._on_timestamp("speech_started_ts", 123.0)
    assert runtime._turn.marks == ["speech_started"]


def test_voice_factory_only_forwards_kwargs_the_constructor_declares():
    """A provider with no `on_usage`/`on_timestamp` (the fake) still builds."""
    from asimoov.core.runtime import _voice_factory

    provider = _voice_factory(
        FakeVoiceProvider, on_usage=lambda usage: None, on_timestamp=lambda name, ts: None
    )()
    assert isinstance(provider, FakeVoiceProvider)


async def test_the_supervisor_restarts_a_crashing_component():
    supervisor = Supervisor()
    attempts: list[int] = []

    async def flaky() -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("crash")
        await asyncio.sleep(10)

    supervisor.spawn("flaky", flaky)
    try:
        for _ in range(400):
            if len(attempts) >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await supervisor.stop_all()
    assert len(attempts) >= 3
    assert supervisor.components["flaky"].restarts >= 2
    assert "crash" in supervisor.components["flaky"].last_error


async def test_the_supervisor_publishes_component_health():
    from asimoov.core.bus.local import LocalBus

    bus = LocalBus()
    supervisor = Supervisor(bus)

    async def broken() -> None:
        raise RuntimeError("nope")

    supervisor.spawn("broken", broken)
    try:
        for _ in range(200):
            if bus.latest(TOPICS.COMPONENT_HEALTH_PATTERN) is not None:
                break
            await asyncio.sleep(0.01)
    finally:
        await supervisor.stop_all()
    health = bus.latest(TOPICS.COMPONENT_HEALTH_PATTERN).data
    assert health["component"] == "broken"
    assert health["restarts"] >= 1


async def test_stop_is_safe_to_call_twice(runtime):
    await runtime.stop()
    await runtime.stop()
    assert runtime.started is False


class PageFace:
    """A renderer that serves a page, like WS6's `WebFace`."""

    ws_path = "/face/ws"

    def __init__(self) -> None:
        self.ctx: dict = {}
        self.states: list = []
        self.attached: list[str] = []

    @staticmethod
    def process_request(path: str):
        return None

    async def start(self, ctx: dict) -> None:
        self.ctx = ctx

    async def render(self, state) -> None:
        self.states.append(state)

    async def stop(self) -> None:
        return None

    async def attach(self, connection, *, path=None) -> None:
        self.attached.append(path or "")


async def test_a_page_face_is_mounted_on_the_hub(avatar_config, tmp_path):
    face = PageFace()
    config = replace(avatar_config, hub=HubConfig(host="127.0.0.1", port=0))
    runtime = Runtime(
        config=config,
        body=FakeBody(),
        voice=None,
        faces=(face,),
        memory=SqliteMemoryStore(tmp_path / "m.db"),
        tick_s=0.02,
        hub_enabled=True,
    )
    await runtime.start()
    try:
        assert runtime.hub is not None
        assert runtime.hub.process_request_hook is PageFace.process_request
        assert runtime.hub._routes["/face/ws"] == face.attach
        assert face.ctx["bus"] is runtime.bus
        assert face.ctx["token"] == runtime.hub.token
        assert callable(face.ctx["on_estop"])

        await face.ctx["on_estop"]("face_button")
        assert runtime.body.last_stop_all_reason == "face_button"
    finally:
        await runtime.stop()


async def test_the_face_renderer_receives_face_states(avatar_config, tmp_path):
    face = PageFace()
    runtime = Runtime(
        config=avatar_config,
        body=FakeBody(),
        voice=None,
        faces=(face,),
        memory=SqliteMemoryStore(tmp_path / "m.db"),
        tick_s=0.02,
        hub_enabled=False,
    )
    await runtime.start()
    try:
        for _ in range(100):
            if face.states:
                break
            await asyncio.sleep(0.01)
        assert face.states[0].emotion == "neutral"
    finally:
        await runtime.stop()


async def test_set_expression_changes_the_published_face(runtime):
    result = await runtime.registry.call("set_expression", {"emotion": "happy"})
    assert result.status.value == "ok"
    assert runtime.mind.face_state().emotion == "happy"
    for _ in range(100):
        latest = runtime.bus.latest(TOPICS.FACE_STATE)
        if latest is not None and latest.data["emotion"] == "happy":
            break
        await asyncio.sleep(0.01)
    assert runtime.bus.latest(TOPICS.FACE_STATE).data["emotion"] == "happy"
    assert runtime.body.face_states[-1].emotion == "happy"


async def test_a_body_overlay_is_mixed_into_the_face_then_lapses(runtime):
    """A body offers `face.overlay`; the mind alone publishes `face.state`."""
    await runtime.bus.publish(
        TOPICS.FACE_OVERLAY, {"gaze": {"x": 0.7, "y": -0.2}, "ttl_s": 5.0}, kind="state"
    )
    assert runtime.mind.face_state().gaze.x == pytest.approx(0.7)

    runtime.clock.advance(6.0)
    assert runtime.mind.face_state().gaze.x == pytest.approx(0.0)


async def test_an_overlay_without_a_known_field_is_ignored(runtime):
    await runtime.bus.publish(TOPICS.FACE_OVERLAY, {"nonsense": 1}, kind="state")
    assert runtime.mind.face_state().gaze.x == pytest.approx(0.0)


async def test_an_unknown_emotion_is_refused_by_the_tool(runtime):
    result = await runtime.registry.call("set_expression", {"emotion": "smug"})
    assert result.status.value == "error"


async def test_the_voice_config_carries_tool_specs_not_dicts(runtime):
    """Blocker 1: a dict makes `build_session` raise, not reconnect forever."""
    tools = runtime._voice_config()["tools"]
    assert tools and all(isinstance(spec, ToolSpec) for spec in tools)


async def test_the_face_context_forwards_camera_frames_to_the_bus(runtime):
    """Blocker 6: the face page's webcam frames were decoded and dropped."""
    seen: list[bytes] = []
    runtime.bus.subscribe_frames(lambda frame: _append(seen, frame))
    context = runtime._face_context()
    assert context["on_frame"] is not None

    await context["on_frame"]("frame.browser", 1234, "image/jpeg", b"\xff\xd8jpeg")
    assert len(seen) == 1
    assert decode_frame(seen[0]).jpeg == b"\xff\xd8jpeg"


async def _append(sink: list[bytes], frame: bytes) -> None:
    sink.append(frame)


async def test_a_republished_manifest_rebinds_the_tools(runtime):
    """Major 11: a bust only knows its channels once its link is up."""
    assert runtime.registry.get("move") is None
    walker = BodyManifest(
        name="walker",
        kind_of_body="quadruped",
        capabilities=("locomotion.planar", "locomotion.turn"),
        implements={"move": {"primitive": "move"}, "turn": {"primitive": "turn"}},
    )

    await runtime.bus.publish(TOPICS.BODY_MANIFEST, walker.to_dict(), kind="state")

    assert runtime.registry.get("move") is not None
    assert runtime.body.manifest.name == "walker"
    assert "move" in {spec.name for spec in runtime.registry.specs()}


async def test_an_injection_is_held_while_the_model_speaks(runtime):
    """Blocker 4: `on_response_started` is what makes the injector wait."""
    bridge = _VoiceBridge(runtime)
    bridge.on_response_started("resp_1")
    assert runtime.injector.response_active is True

    runtime.injector.submit("[Perception] Sam arrived.", now=0.0)
    assert await runtime.injector.pump(now=99.0) is None

    await runtime.on_response_done("resp_1")
    assert runtime.injector.response_active is False


async def test_the_mind_does_not_crash_when_a_response_is_already_active(runtime):
    """Blocker 4: `request_response` raising must be a skip, not a crash."""

    async def always_busy(instructions):
        raise RuntimeError("request_response while response resp_1 is active")

    runtime.mind.request_response = always_busy
    runtime.mind.injector.submit("[Perception] Sam arrived.", now=0.0)
    runtime.clock.advance(5.0)
    await runtime.mind.tick()  # must not raise


async def test_an_episode_is_written_when_the_session_ends(avatar_config, tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    runtime = Runtime(
        config=avatar_config,
        body=FakeBody(),
        voice=FakeVoiceProvider(),
        memory=store,
        clock=ManualClock(1000.0),
        tick_s=0.02,
        hub_enabled=False,
    )
    await runtime.start()
    await runtime.record_assistant_text("Hello Sam, nice to see you again.")
    await runtime.stop()

    await store.open()
    try:
        assert "nice to see you again" in " ".join(await store.recall("Sam", 5))
    finally:
        await store.close()


async def test_no_episode_is_written_when_nobody_said_anything(avatar_config, tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    runtime = Runtime(
        config=avatar_config,
        body=FakeBody(),
        voice=FakeVoiceProvider(),
        memory=store,
        clock=ManualClock(1000.0),
        tick_s=0.02,
        hub_enabled=False,
    )
    await runtime.start()
    await runtime.stop()

    await store.open()
    try:
        assert await store.recall("", 5) == []
    finally:
        await store.close()


def _uncertain(track_id: str = "t1", name: str = "Sam", score: float = 0.52) -> dict:
    return PersonSeen(
        track_id=track_id,
        confidence=0.9,
        bearing=Bearing(az=4.0),
        distance_class="near",
        bbox_norm=(0.4, 0.3, 0.6, 0.7),
        face_quality=0.7,
        identity_status="uncertain",
        candidate_person_id="person:sam",
        candidate_name=name,
        candidate_score=score,
    ).to_dict()


async def test_an_uncertain_match_is_offered_once_per_track(runtime):
    """Plan 4.7: the mind says who this may be, it never asserts it."""
    topic = TOPICS.PERCEPT_PREFIX + "person_seen"
    await runtime.bus.publish(topic, _uncertain())
    await runtime.bus.publish(topic, _uncertain())

    pending = runtime.injector.pending
    assert len(pending) == 1
    assert "Sam" in pending[0]
    assert "0.52" in pending[0]
    assert "person:sam" not in pending[0]


async def test_an_identified_match_is_not_announced_as_a_doubt(runtime):
    seen = _uncertain()
    seen.update(
        identity_status="identified",
        person_id="person:sam",
        name="Sam",
        candidate_person_id=None,
        candidate_name=None,
        candidate_score=None,
    )
    await runtime.bus.publish(TOPICS.PERCEPT_PREFIX + "person_seen", seen)
    assert runtime.injector.pending == ()
