"""LLM tool contracts: what the model can call, and what it gets back.

Tools are generated dynamically from behaviors, memory operations and app
`provides.tools` (plan.md section 4.3): `gesture(name)`, `move(...)`,
`turn(...)`, `look_at(target)`, `set_expression(emotion)`, `stop()`,
`who_is_here()`, `remember_person(name)`, `remember_fact(person, fact)`,
`recall(query)`, plus whatever apps register.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from asimoov.contracts.behaviors import BehaviorResult, BehaviorStatus
from asimoov.contracts.vocab import DURATION_CLASSES


@dataclass(frozen=True)
class ToolSpec:
    """Static description of an LLM-callable tool.

    Raises:
        ValueError: if ``duration_class`` is not in ``vocab.DURATION_CLASSES``.
    """

    name: str
    description: str
    params: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    timeout_s: float = 2.0
    duration_class: str = "short"

    def __post_init__(self) -> None:
        if self.duration_class not in DURATION_CLASSES:
            raise ValueError(f"invalid duration_class: {self.duration_class!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "params": self.params,
            "timeout_s": self.timeout_s,
            "duration_class": self.duration_class,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolSpec:
        return cls(
            name=payload["name"],
            description=payload["description"],
            params=payload.get("params", {"type": "object", "properties": {}}),
            timeout_s=payload.get("timeout_s", 2.0),
            duration_class=payload.get("duration_class", "short"),
        )


@dataclass(frozen=True)
class ToolResult:
    """What a tool call resolves to, sent back to the LLM as function output.

    Shares its status vocabulary with `BehaviorResult` since most tools
    simply dispatch to a behavior; `content` replaces `BehaviorResult.data`
    as the field name to make clear this is LLM-visible text/JSON, never a
    raw sensor value.

    Raises:
        ValueError: if ``status`` is ``error`` without a ``reason``, or
            ``started`` without an ``action_id``.
    """

    status: BehaviorStatus
    content: dict[str, Any] = field(default_factory=dict)
    action_id: str | None = None
    eta_s: float | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, BehaviorStatus):
            object.__setattr__(self, "status", BehaviorStatus(self.status))
        if self.status is BehaviorStatus.ERROR and not self.reason:
            raise ValueError("ToolResult(status=error) requires a reason")
        if self.status is BehaviorStatus.STARTED and not self.action_id:
            raise ValueError("ToolResult(status=started) requires an action_id")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status.value}
        if self.content:
            payload["content"] = self.content
        if self.action_id is not None:
            payload["action_id"] = self.action_id
        if self.eta_s is not None:
            payload["eta_s"] = self.eta_s
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolResult:
        return cls(
            status=BehaviorStatus(payload["status"]),
            content=payload.get("content", {}),
            action_id=payload.get("action_id"),
            eta_s=payload.get("eta_s"),
            reason=payload.get("reason"),
        )

    @classmethod
    def from_behavior_result(cls, result: BehaviorResult) -> ToolResult:
        """Convert a `BehaviorResult` into the shape sent back to the LLM."""
        return cls(
            status=result.status,
            content=result.data,
            action_id=result.action_id,
            eta_s=result.eta_s,
            reason=result.reason,
        )


class ToolHandler(Protocol):
    """The callable a `ToolSpec` is bound to at runtime."""

    async def __call__(self, params: dict[str, Any]) -> ToolResult:
        """Execute the tool.

        Must respect the bound `ToolSpec.timeout_s` itself or be wrapped by
        the caller in `wait_for`; either way, exceeding the timeout must
        surface as ``ToolResult(status="timeout")``, never a hang.
        """
