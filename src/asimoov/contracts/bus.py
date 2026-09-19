"""Bus contract (plan.md section 4.3): publish/subscribe/request over topics.

The `Transport` ABC (local in-process queue vs. remote hub WebSocket) lives
in core (WS1), not here: contracts only describes the `Bus` surface every
producer/consumer programs against, so `BodyContext.bus` and
`PerceptionContext.bus` can be typed precisely without `contracts` gaining a
dependency on WS1.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from asimoov.contracts.envelope import Envelope

EnvelopeHandler = Callable[[Envelope], Awaitable[None]]


class Subscription(Protocol):
    """Handle returned by `Bus.subscribe`; drop it to stop receiving."""

    def unsubscribe(self) -> None:
        """Stop the associated handler from receiving further envelopes.

        Idempotent: calling it more than once must not raise.
        """
        ...


@runtime_checkable
class Bus(Protocol):
    """The publish/subscribe/request surface `BodyContext.bus` and
    `PerceptionContext.bus` are typed as (local in-process or remote hub,
    plan.md section 4.3). Implementations live in core (WS1).

    Pattern syntax for `subscribe`: an exact topic (``"body.health"``) or a
    prefix ending in ``*`` (``"percept.*"`` matches every ``percept.<type>``
    topic, including the bare ``"percept."`` topic itself). No other
    wildcard forms.
    """

    async def publish(
        self,
        topic: str,
        data: Mapping[str, Any],
        *,
        kind: str = "percept",
        corr: str | None = None,
    ) -> None:
        """Publish ``data`` on ``topic`` as an envelope of the given ``kind``."""
        ...

    def subscribe(self, pattern: str, handler: EnvelopeHandler) -> Subscription:
        """Call ``handler`` for every envelope whose topic matches ``pattern``."""
        ...

    async def request(self, topic: str, data: Mapping[str, Any], *, timeout_s: float) -> Envelope:
        """Publish on ``topic`` and wait for the matching ``reply`` envelope.

        Raises:
            asyncio.TimeoutError: if no reply arrives within ``timeout_s``.
        """
        ...

    def latest(self, topic: str) -> Envelope | None:
        """Return the last envelope published on exactly ``topic``.

        None if nothing has been published on ``topic`` yet. Non-blocking.
        """
        ...
