"""The shared conformance suite, against the protocol simulator."""

from __future__ import annotations

from fake_firmware import BUST_CHANNELS, FakeFirmware, FakeSerialPort, make_tcp_body

from asimoov.bodies.conformance import run_conformance
from asimoov.bodies.inmoov.adapter import InMoovBody
from asimoov.bodies.inmoov.link import SerialLink
from asimoov.contracts.body import BodyContext


async def test_conformance_over_tcp(bench_firmware: FakeFirmware) -> None:
    body, server = await make_tcp_body(bench_firmware)
    try:
        await run_conformance(body)
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_conformance_over_serial(bench_firmware: FakeFirmware) -> None:
    link = SerialLink("fake", serial_factory=lambda: FakeSerialPort(bench_firmware))
    body = InMoovBody({}, link=link)
    try:
        await run_conformance(body)
    finally:
        await body.stop()


async def test_manifest_follows_the_bench(bench_firmware: FakeFirmware) -> None:
    body, server = await make_tcp_body(bench_firmware)
    try:
        await body.start(BodyContext())
        assert body.manifest.kind_of_body == "humanoid_bust"
        assert "gesture.finger_demo" in body.manifest.capabilities
        assert "gesture.hand_right" not in body.manifest.capabilities
        assert "gaze.pan_tilt" not in body.manifest.capabilities
        assert set(body.manifest.implements) == {"finger_demo"}
        assert body.manifest.limits["joints"] == {"finger_demo": [2, 108]}
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_manifest_follows_the_bust() -> None:
    firmware = FakeFirmware(channels=BUST_CHANNELS)
    body, server = await make_tcp_body(firmware)
    try:
        await body.start(BodyContext())
        capabilities = set(body.manifest.capabilities)
        assert {"gesture.hand_right", "gaze.pan_tilt", "face.jaw", "face.eyelids"} <= capabilities
        assert set(body.manifest.implements) == {
            "shake_hand",
            "wave_hello",
            "nod",
            "shake_head",
            "point",
        }
        assert body.manifest.implements["shake_hand"]["est_ms"] == 2800
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()


async def test_move_is_unsupported(bench_firmware: FakeFirmware) -> None:
    body, server = await make_tcp_body(bench_firmware)
    try:
        await body.start(BodyContext())
        result = await body.move(1.0, 0.0, 0.0, 1.0)
        assert result.status.value == "unsupported"
    finally:
        await body.stop()
        server.close()
        await server.wait_closed()
