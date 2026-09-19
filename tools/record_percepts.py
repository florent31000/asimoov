#!/usr/bin/env python
"""Record a live session's percepts into a replay file.

Connects to a running robot's hub like any other module and appends every
matching envelope to a JSONL file, which `asimoov replay` can then play
back through the core. Percepts only by default: no audio, no frames, no
embeddings.

    python tools/record_percepts.py out.jsonl --topics "percept.*"
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
from pathlib import Path

from asimoov.contracts.envelope import Envelope
from asimoov.core.bus.client import BusClient
from asimoov.core.bus.hub import read_or_create_token


async def record(path: Path, url: str, token: str, topics: tuple[str, ...]) -> int:
    client = BusClient(url, token, "tools.record_percepts", subscriptions=topics)
    count = 0
    stop = asyncio.Event()

    with path.open("a", encoding="utf-8") as handle:

        async def on_envelope(envelope: Envelope) -> None:
            nonlocal count
            handle.write(json.dumps(envelope.to_dict(), ensure_ascii=False) + "\n")
            handle.flush()
            count += 1

        for pattern in topics:
            client.subscribe(pattern, on_envelope)
        await client.connect()
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(getattr(signal, name), stop.set)
        print(f"recording {', '.join(topics)} to {path} (Ctrl+C to stop)")
        try:
            await stop.wait()
        except KeyboardInterrupt:
            pass
        finally:
            await client.close()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="JSONL file to append to")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7331)
    parser.add_argument("--topics", default="percept.*", help="comma-separated patterns")
    parser.add_argument("--token", default=None, help="hub token (default: ~/.asimoov/token)")
    args = parser.parse_args()

    topics = tuple(topic.strip() for topic in args.topics.split(",") if topic.strip())
    url = f"ws://{args.host}:{args.port}/bus"
    token = args.token or read_or_create_token()
    try:
        count = asyncio.run(record(Path(args.output), url, token, topics))
    except KeyboardInterrupt:
        return 130
    print(f"recorded {count} envelopes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
