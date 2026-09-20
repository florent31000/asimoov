"""ASIMOOV command line: `run`, `replay`, `enroll`, `stats`, `doctor`, `mcp`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sqlite3
import sys
from pathlib import Path

from asimoov.contracts.vocab import TOPICS
from asimoov.core.bus.client import BusClient, read_token
from asimoov.core.clock import ScaledClock, wall_clock
from asimoov.core.config import ConfigError, Secrets, asimoov_home, load_robot_config
from asimoov.core.logging_setup import setup_logging
from asimoov.core.memory.sqlite_store import person_id_for_name
from asimoov.core.plugins import (
    BODY_GROUP,
    FACE_GROUP,
    PERCEPTION_GROUP,
    VOICE_GROUP,
    PluginError,
    available,
)
from asimoov.core.replay import (
    DEFAULT_MAX_GAP_S,
    ReplayAssertionError,
    Replayer,
    ReplayError,
    first_timestamp,
)
from asimoov.core.runtime import Runtime
from asimoov.core.telemetry import Telemetry, format_stats, metrics_dir, read_metrics, summarize
from asimoov.core.tools.mcp_export import serve_stdio

DEFAULT_REPLAY_SPEED = 1.0
HUB_HOST = "127.0.0.1"
HUB_PORT = 7331
CONNECT_TIMEOUT_S = 5.0
ENROLL_TIMEOUT_S = 12.0
SCENE_WAIT_S = 2.0
ENROLL_TOPIC = "perception.face_id.enroll"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asimoov", description="The open-source soul for companion robots")
    parser.add_argument("--log-level", default="INFO", help="root log level (default: INFO)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a robot")
    run.add_argument("robot_dir", help="directory containing robot.yaml")
    run.add_argument("--voice", help="voice provider name, or 'none'")
    run.add_argument("--body", help="body name (overrides robot.yaml)")
    run.add_argument("--face", action="append", help="face renderer name, or 'none' (repeatable)")
    run.add_argument("--replay", help="drive this run from a replay file instead of live percepts")
    run.add_argument("--speed", type=float, default=DEFAULT_REPLAY_SPEED, help="replay speed")
    run.add_argument("--max-gap", type=float, default=DEFAULT_MAX_GAP_S, help="longest real wait between two replayed envelopes")
    run.add_argument("--no-hub", action="store_true", help="do not open the WebSocket hub")

    replay = sub.add_parser("replay", help="replay a percept stream and check its assertions")
    replay.add_argument("file", help="JSONL replay file")
    replay.add_argument("--robot", default="robots/avatar", help="robot directory to run it against")
    replay.add_argument("--speed", type=float, default=DEFAULT_REPLAY_SPEED)
    replay.add_argument("--max-gap", type=float, default=DEFAULT_MAX_GAP_S)
    replay.add_argument("--body", default="fake")
    replay.add_argument("--voice", default="fake")
    replay.add_argument("--face", action="append", default=None)

    stats = sub.add_parser("stats", help="print latency percentiles from ~/.asimoov/metrics")
    stats.add_argument("--metrics-dir", default=None)

    enroll = sub.add_parser("enroll", help="teach the running robot a face it can see")
    enroll.add_argument("--name", required=True, help="who this person is")
    enroll.add_argument("--robot", default=None, help="robot directory, for the hub host/port")
    enroll.add_argument("--hub", default=None, help="hub URL, e.g. ws://192.168.1.20:7331")
    enroll.add_argument("--track", default=None, help="track id, when several people are visible")
    enroll.add_argument("--timeout", type=float, default=ENROLL_TIMEOUT_S)

    doctor = sub.add_parser("doctor", help="check the installation and a robot configuration")
    doctor.add_argument("robot_dir", nargs="?", default=None)
    doctor.add_argument(
        "--download-models",
        action="store_true",
        help="download the perception models to ~/.asimoov/models before reporting",
    )

    mcp = sub.add_parser("mcp", help="expose this robot's tools as an MCP server on stdio")
    mcp.add_argument("robot_dir", help="directory containing robot.yaml")
    mcp.add_argument("--body", default="fake")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging_setup = setup_logging(args.log_level.upper())
    try:
        if args.command == "run":
            return asyncio.run(_run(args))
        if args.command == "replay":
            return asyncio.run(_replay(args))
        if args.command == "enroll":
            return asyncio.run(_enroll(args))
        if args.command == "stats":
            return _stats(args)
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "mcp":
            return asyncio.run(_mcp(args))
    except (ConfigError, PluginError, ReplayError) as exc:
        print(f"asimoov: {exc}", file=sys.stderr)
        return 2
    except ReplayAssertionError as exc:
        print(f"asimoov: replay assertion failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        logging_setup.stop()
    return 2


async def _build(
    args,
    *,
    replay_file: str | None,
    speed: float,
    hub_enabled: bool,
    default_faces: tuple[str, ...] | None = None,
    default_voice: str | None = None,
) -> Runtime:
    config = load_robot_config(getattr(args, "robot_dir", None) or args.robot)
    clock = wall_clock
    tick_s = 1.0
    if replay_file:
        origin = first_timestamp(replay_file)
        if origin is None:
            raise ReplayError(f"{replay_file}: contains no envelope to replay")
        clock = ScaledClock(origin=origin, speed=speed)
        tick_s = 1.0 / speed
    faces = tuple(args.face) if getattr(args, "face", None) else default_faces
    runtime = Runtime.build(
        config,
        body_name=getattr(args, "body", None),
        voice_name=getattr(args, "voice", None) or default_voice,
        face_names=faces,
        telemetry=Telemetry(),
        clock=clock,
        tick_s=tick_s,
        hub_enabled=hub_enabled,
    )
    await runtime.start()
    if isinstance(clock, ScaledClock):
        clock.reset()
    return runtime


async def _run(args) -> int:
    runtime = await _build(
        args, replay_file=args.replay, speed=args.speed, hub_enabled=not args.no_hub
    )
    try:
        if args.replay:
            result = await Replayer(
                runtime.bus, speed=args.speed, clock=runtime.clock, max_gap_s=args.max_gap
            ).run(args.replay)
            print(
                f"replayed {result.envelopes} envelopes, {result.assertions} assertions passed "
                f"in {result.duration_s:.1f}s"
            )
            return 0
        print(f"asimoov running; hub on port {runtime.config.hub.port}. Ctrl+C to stop.")
        with contextlib.suppress(asyncio.CancelledError):
            await runtime.run_forever()
        return 0
    finally:
        await runtime.stop()


async def _replay(args) -> int:
    runtime = await _build(
        args,
        replay_file=args.file,
        speed=args.speed,
        hub_enabled=False,
        default_faces=(),
    )
    try:
        result = await Replayer(
            runtime.bus, speed=args.speed, clock=runtime.clock, max_gap_s=args.max_gap
        ).run(args.file)
        print(
            f"replayed {result.envelopes} envelopes, {result.assertions} assertions passed "
            f"in {result.duration_s:.1f}s"
        )
        return 0
    finally:
        await runtime.stop()


async def _enroll(args) -> int:
    """Ask the running perception process to learn the visible face.

    The robot must already be running: this talks to its hub, it does not
    open a camera itself.
    """
    url = args.hub
    if url is None:
        host, port = HUB_HOST, HUB_PORT
        if args.robot:
            hub = load_robot_config(args.robot).hub
            host, port = hub.host, hub.port
        url = f"ws://{host}:{port}"
    client = BusClient(
        url, read_token(), "cli.enroll", subscriptions=(TOPICS.SCENE_STATE,)
    )
    try:
        await client.connect(min(CONNECT_TIMEOUT_S, args.timeout))
    except (TimeoutError, asyncio.TimeoutError):
        print(f"asimoov: no robot answering on {url}", file=sys.stderr)
        return 2
    try:
        track_id = args.track or await _visible_track(client)
        if track_id is None:
            print(
                "asimoov: nobody is visible right now (or several people are: pass --track)",
                file=sys.stderr,
            )
            return 1
        print(f"enrolling {args.name} on track {track_id}, hold still...")
        reply = await client.request(
            ENROLL_TOPIC,
            {
                "track_id": track_id,
                "person_id": person_id_for_name(args.name),
                "name": args.name,
            },
            timeout_s=args.timeout,
        )
    except (TimeoutError, asyncio.TimeoutError):
        print(f"asimoov: perception did not answer within {args.timeout:.0f}s", file=sys.stderr)
        return 1
    finally:
        await client.close()

    if not reply.data.get("ok"):
        print(f"asimoov: enrollment failed: {reply.data.get('reason')}", file=sys.stderr)
        return 1
    print(f"enrolled {args.name} ({reply.data.get('samples', 0)} samples)")
    return 0


async def _visible_track(client: BusClient) -> str | None:
    """The one visible person's track id, or None if there is not exactly one.

    The hub replays its retained `scene.state` just after the handshake, so
    this waits a moment for it rather than deciding on an empty scene.
    """
    deadline = asyncio.get_running_loop().time() + SCENE_WAIT_S
    while True:
        scene = client.latest(TOPICS.SCENE_STATE)
        people = scene.data.get("people", []) if scene is not None else []
        if len(people) == 1:
            return people[0]["track_id"]
        if len(people) > 1 or asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.05)


def _stats(args) -> int:
    directory = Path(args.metrics_dir) if args.metrics_dir else metrics_dir()
    print(format_stats(summarize(read_metrics(directory))))
    return 0


def _download_models() -> list[str]:
    """Fetch the perception models, reporting the outcome rather than raising."""
    from asimoov.perception.models import download_models

    try:
        paths = download_models()
    except Exception as exc:  # noqa: BLE001 - doctor reports, it never crashes
        return [f"model download   FAILED: {exc}"]
    return [f"model download   {len(paths)} file(s) in {paths[0].parent}"] if paths else []


def _models_report() -> list[str]:
    """Which ONNX models are on disk, and what works without them."""
    from asimoov.perception.models import DETECTOR, EMBEDDER, SILERO_VAD, is_present, models_dir

    lines = [f"models directory {models_dir()}"]
    face_ready = is_present(DETECTOR) and is_present(EMBEDDER)
    lines.append(
        "face models      "
        + (
            "present (recognition available)"
            if face_ready
            else "missing: no face recognition (asimoov doctor --download-models)"
        )
    )
    lines.append(
        "silero vad       "
        + (
            "present"
            if is_present(SILERO_VAD)
            else "missing (asimoov doctor --download-models)"
        )
    )
    try:
        import onnxruntime  # noqa: F401

        onnx = True
    except ImportError:
        onnx = False
    lines.append(
        "vad extra        "
        + ("installed" if onnx else "missing (pip install asimoov[vad])")
    )
    if onnx and is_present(SILERO_VAD):
        mode = "full-duplex, Silero VAD"
    else:
        mode = "full-duplex, relative-energy detector (less precise)"
    lines.append(f"barge-in         {mode}")
    return lines


def _audio_report() -> list[str]:
    """Which capture/playback backend this machine can actually open."""
    backends = []
    try:
        import sounddevice  # noqa: F401

        backends.append("sounddevice (desktop)")
    except Exception:  # noqa: BLE001 - a missing PortAudio raises OSError, not ImportError
        pass
    try:
        import jnius  # noqa: F401

        backends.append("android AudioRecord/AudioTrack")
    except Exception:  # noqa: BLE001
        pass
    return [
        "audio backends   "
        + (", ".join(backends) or "none: no microphone or speaker (pip install asimoov[desktop])")
    ]


def _claude_report() -> list[str]:
    """One line per piece the Claude pipeline needs, and why it is missing."""
    import shutil

    from asimoov.voice.claude_pipeline.stt import FasterWhisperSTT
    from asimoov.voice.claude_pipeline.tts import KOKORO_DEFAULTS, KokoroTTS

    lines = []
    try:
        import anthropic

        lines.append(f"anthropic sdk    {anthropic.__version__}")
    except ImportError:
        lines.append("anthropic sdk    missing (pip install asimoov[claude])")
    lang, voice = KOKORO_DEFAULTS["en"]
    engines = (("stt", FasterWhisperSTT()), ("tts", KokoroTTS(lang=lang, voice=voice)))
    for label, engine in engines:
        ok, reason = engine.available()
        lines.append(f"claude {label}       " + ("available: " if ok else "NOT READY: ") + reason)
    espeak = shutil.which("espeak-ng")
    lines.append(
        "espeak-ng        "
        + (f"on PATH ({espeak})" if espeak else "NOT on PATH (kokoro-onnx needs it to phonemize)")
    )
    return lines


def _audio_plan_report(config) -> tuple[list[str], bool]:
    """Whether this robot would actually open a microphone and a speaker.

    A robot whose body offers no audio device and whose host has no backend
    is deaf and mute: `doctor` says so and fails, rather than printing a
    tidy report of a robot that cannot hold a conversation.
    """
    from asimoov.core.plugins import load_plugin
    from asimoov.core.voice_loop import plan_audio

    body = load_plugin(BODY_GROUP, config.body.type)()
    plan = plan_audio(config.audio, body)
    if plan.ready:
        return [f"audio            {plan.describe()}"], True
    return [f"audio            NOT READY: {plan.describe()}"], False


def _doctor(args) -> int:
    lines = [f"python           {sys.version.split()[0]}"]
    lines.append(f"state directory  {asimoov_home()}")
    if args.download_models:
        lines.extend(_download_models())
    lines.append(f"bodies           {', '.join(available(BODY_GROUP)) or 'none'}")
    lines.append(f"voice providers  {', '.join(available(VOICE_GROUP)) or 'none'}")
    lines.append(f"faces            {', '.join(available(FACE_GROUP)) or 'none'}")
    lines.append(f"perception       {', '.join(available(PERCEPTION_GROUP)) or 'none'}")

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(x)")
        lines.append("sqlite fts5      available")
    except sqlite3.OperationalError:
        lines.append("sqlite fts5      MISSING: memory recall will not work")
    finally:
        connection.close()

    lines.extend(_models_report())
    lines.extend(_audio_report())

    lines.extend(_claude_report())

    secrets = Secrets()
    for key, label in (("openai_api_key", "openai key"), ("anthropic_api_key", "anthropic key")):
        # Whether it is set, never the value.
        state = "set" if secrets.get(key) else f"not set ({secrets.env_var(key)})"
        lines.append(f"{label:<17}{state}")

    ready = True
    if args.robot_dir:
        config = load_robot_config(args.robot_dir)
        lines.append(f"robot            {config.source}")
        lines.append(f"persona          {config.persona.name} ({config.persona.language})")
        lines.append(f"body             {config.body.type}")
        lines.append(f"faces            {', '.join(config.faces) or 'none'}")
        lines.append(f"memory           {config.memory_file()}")
        audio_lines, ready = _audio_plan_report(config)
        lines.extend(audio_lines)
    print("\n".join(lines))
    return 0 if ready else 1


async def _mcp(args) -> int:
    runtime = await _build(
        args,
        replay_file=None,
        speed=1.0,
        hub_enabled=False,
        default_faces=(),
        default_voice="none",
    )
    try:
        await serve_stdio(runtime.registry)
        return 0
    finally:
        await runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())
