"""`Transport`: how a `LocalBus` reaches envelopes that live in another process.

A bus with no transport is purely in-process. Attaching one (the WebSocket
client of `bus/client.py` today, Zenoh or MQTT later, plan.md section 4.1)
forwards every locally published envelope outward and injects every
envelope received from the far side into the local dispatch, without
re-forwarding it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from asimoov.contracts.envelope import Envelope

InboundHandler = Callable[[Envelope], Awaitable[None]]
InboundFrameHandler = Callable[[bytes], Awaitable[None]]


class Transport(ABC):
    """A link carrying envelopes between a `LocalBus` and the outside world."""

    @abstractmethod
    async def start(self, inbound: InboundHandler) -> None:
        """Connect and call ``inbound`` for every envelope received.

        Must not block for more than 100 ms: reconnection runs in an
        internal task, like `Body.start`.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Close the link. Idempotent."""

    @abstractmethod
    async def send(self, envelope: Envelope) -> None:
        """Forward a locally published envelope to the far side.

        Never raises when the link is down: a transport drops envelopes it
        cannot deliver and reports the loss through its own logging.
        """

    # -- binary frames ----------------------------------------------------
    #
    # `frame.*` messages (`contracts.frames`) travel beside the envelopes, as
    # raw binary: they are too big and too frequent to be base64'd into JSON,
    # and the core never reads them. A transport with no binary channel keeps
    # the defaults below and frames simply stay local.

    async def send_frame(self, frame: bytes) -> None:
        """Forward a locally published binary frame to the far side."""
        return None

    def set_frame_handler(self, handler: InboundFrameHandler | None) -> None:
        """Register the coroutine called for every binary frame received."""
        return None
