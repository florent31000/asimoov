"""`Go2Body` against the frozen conformance suite, on a mocked data channel."""

from __future__ import annotations

from asimoov.bodies.conformance import run_conformance
from asimoov.bodies.go2.adapter import Go2Body


async def test_conformance(body: Go2Body) -> None:
    await run_conformance(body)
