"""The process wiring: hello, command routing, replies, scoped envelope `src`."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from helpers import FakeHub, RecordingBus

from asimoov.contracts.bus import Bus
from asimoov.contracts.perception import PerceptionContext, PerceptionModule
from asimoov.core.bus.client import BusClient
from asimoov.perception import PerceptionError
from asimoov.perception.__main__ import HEALTH_TOPIC, Process, ScopedBus, build_parser
from asimoov.perception.config import FaceIdConfig, PerceptionConfig


class StubModule(PerceptionModule):
    def __init__(self, command: str = "face_id.enroll") -> None:
        self.command = command
        self.ctx: PerceptionContext | None = None
        self.stopped = False
        self.calls: list[dict[str, Any]] = []

    async def start(self, ctx: PerceptionContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        self.stopped = True

    async def handle_command(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name != self.command:
            raise KeyError(name)
        self.calls.append(params)
        return {"ok": True, "samples": 5}


def config(**kwargs) -> PerceptionConfig:
    return PerceptionConfig(face_id=FaceIdConfig(enabled=True, **kwargs))


async def start_process(hub_url: str, module: StubModule, monkeypatch) -> tuple[Process, BusClient]:
    client = BusClient(hub_url, module_id="perception")
    process = Process(config(), hub=client)
    monkeypatch.setattr(Process, "_build_face_id", lambda self: module)
    await process.start()
    return process, client


def test_the_cli_exposes_hub_and_config():
    args = build_parser().parse_args(["--hub", "ws://h:7331", "--config", "robot.yaml"])
    assert args.hub == "ws://h:7331"
    assert args.config == "robot.yaml"
    assert args.module_id == "perception"


async def test_no_enabled_module_is_an_explicit_error():
    async with FakeHub() as hub:
        process = Process(PerceptionConfig(), hub=BusClient(hub.url))
        with pytest.raises(PerceptionError, match="no perception module"):
            await process.start()
        await process.stop()


async def test_the_process_announces_itself_and_subscribes_to_its_commands(monkeypatch):
    async with FakeHub() as hub:
        process, _ = await start_process(hub.url, StubModule(), monkeypatch)
        await process.stop()
    assert hub.hello == {
        "type": "hello",
        "module_id": "perception",
        "subscribe": ["perception.*"],
    }


async def test_an_enrol_command_is_routed_and_replied_to(monkeypatch):
    from asimoov.contracts.envelope import Envelope

    module = StubModule()
    async with FakeHub() as hub:
        process, _ = await start_process(hub.url, module, monkeypatch)
        cmd = Envelope(
            kind="cmd",
            topic="perception.face_id.enroll",
            src="core",
            data={"track_id": "t1", "person_id": "p_sam"},
        )
        await hub.push(cmd)
        await hub.wait_for(1)
        await process.stop()
    reply = hub.received[0]
    assert reply["kind"] == "reply"
    assert reply["corr"] == cmd.id
    assert reply["data"] == {"ok": True, "samples": 5}
    assert module.calls == [{"track_id": "t1", "person_id": "p_sam"}]


async def test_an_unknown_command_still_gets_a_reply(monkeypatch):
    from asimoov.contracts.envelope import Envelope

    async with FakeHub() as hub:
        process, _ = await start_process(hub.url, StubModule(), monkeypatch)
        await hub.push(Envelope(kind="cmd", topic="perception.face_id.nope", src="core"))
        await hub.wait_for(1)
        await process.stop()
    assert hub.received[0]["data"]["ok"] is False
    assert "unknown command" in hub.received[0]["data"]["reason"]


async def test_a_failing_command_replies_with_the_reason(monkeypatch):
    from asimoov.contracts.envelope import Envelope

    class Failing(StubModule):
        async def handle_command(self, name, params):
            raise RuntimeError("camera gone")

    async with FakeHub() as hub:
        process, _ = await start_process(hub.url, Failing(), monkeypatch)
        await hub.push(Envelope(kind="cmd", topic="perception.face_id.enroll", src="core"))
        await hub.wait_for(1)
        await process.stop()
    assert hub.received[0]["data"] == {"ok": False, "reason": "RuntimeError: camera gone"}


async def test_a_slow_command_does_not_block_the_receive_loop(monkeypatch):
    from asimoov.contracts.envelope import Envelope

    class Slow(StubModule):
        async def handle_command(self, name, params):
            await asyncio.sleep(0.4)
            return {"ok": True}

    async with FakeHub() as hub:
        process, client = await start_process(hub.url, Slow(), monkeypatch)
        await hub.push(Envelope(kind="cmd", topic="perception.face_id.enroll", src="core"))
        await asyncio.sleep(0.05)
        await client.publish("percept.person_seen", {"type": "person_seen"})
        await hub.wait_for(1)
        assert hub.received[0]["kind"] == "percept"  # the percept overtook the slow reply
        await hub.wait_for(2)
        await process.stop()
    assert hub.received[1]["kind"] == "reply"


async def test_percepts_are_published_under_the_module_src(monkeypatch):
    module = StubModule()
    async with FakeHub() as hub:
        process, _ = await start_process(hub.url, module, monkeypatch)
        await module.ctx.publish("percept.person_seen", {"type": "person_seen"})
        await hub.wait_for(1)
        await process.stop()
    assert hub.received[0]["src"] == "perception.face_id"


async def test_the_scoped_bus_is_a_bus():
    assert isinstance(ScopedBus(BusClient("ws://127.0.0.1:7331"), "perception.face_id"), Bus)


async def test_stopping_stops_every_module(monkeypatch):
    module = StubModule()
    async with FakeHub() as hub:
        process, _ = await start_process(hub.url, module, monkeypatch)
        await process.stop()
    assert module.stopped is True


async def test_a_recording_bus_stands_in_for_the_hub_in_module_tests():
    bus = RecordingBus()
    await bus.publish("percept.person_seen", {"type": "person_seen"})
    assert bus.topics() == ["percept.person_seen"]


async def test_a_module_that_fails_to_stop_is_reported_not_swallowed(monkeypatch):
    class Breaking(StubModule):
        async def stop(self):
            raise RuntimeError("camera stuck")

    async with FakeHub() as hub:
        process, _ = await start_process(hub.url, Breaking(), monkeypatch)
        stopping = asyncio.create_task(process.stop())
        await hub.wait_for(1)
        await stopping
    assert process.errors == ["face_id.stop: RuntimeError: camera stuck"]
    assert hub.received[0]["topic"] == HEALTH_TOPIC
    assert hub.received[0]["kind"] == "state"
    assert hub.received[0]["data"]["errors"] == ["face_id.stop: RuntimeError: camera stuck"]


async def test_stopping_a_healthy_process_records_no_error(monkeypatch):
    async with FakeHub() as hub:
        process, _ = await start_process(hub.url, StubModule(), monkeypatch)
        await process.stop()
    assert process.health() == {"module_id": "perception", "modules": ["face_id"], "errors": []}
