"""Behavior manifests, calls and results (behavior.v1).

A behavior is a semantic action ("shake_hand") that each body translates in
its own way (see ``contracts/body.py``, ``BodyManifest.implements``). See
``schemas/behavior.v1.json`` and ``examples/behavior.shake_hand.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from asimoov.contracts.envelope import new_id
from asimoov.contracts.vocab import DURATION_CLASSES, is_capability


class BehaviorStatus(str, Enum):
    """Outcome of a behavior invocation, returned to the LLM as a tool result."""

    OK = "ok"
    STARTED = "started"
    ERROR = "error"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class BehaviorManifest:
    """Static description of a behavior, loaded from a `behaviors/*.yaml` file.

    Raises:
        ValueError: if ``duration_class`` is not in ``vocab.DURATION_CLASSES``,
            or if any entry of ``requires`` is not a known capability
            (``vocab.is_capability``).
    """

    name: str
    version: int
    description: str
    duration_class: str
    params: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    requires: tuple[str, ...] = ()
    interruptible: bool = True
    fallback: str | None = None
    llm_visible: bool = True
    api_version: str = "asimoov/v1"

    def __post_init__(self) -> None:
        if self.duration_class not in DURATION_CLASSES:
            raise ValueError(f"invalid duration_class: {self.duration_class!r}")
        for capability in self.requires:
            if not is_capability(capability):
                raise ValueError(f"unknown capability in requires: {capability!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": self.api_version,
            "kind": "Behavior",
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "params": self.params,
            "requires": list(self.requires),
            "duration_class": self.duration_class,
            "interruptible": self.interruptible,
            "fallback": self.fallback,
            "llm_visible": self.llm_visible,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BehaviorManifest:
        return cls(
            name=payload["name"],
            version=payload["version"],
            description=payload["description"],
            duration_class=payload["duration_class"],
            params=payload.get("params", {"type": "object", "properties": {}}),
            requires=tuple(payload.get("requires", [])),
            interruptible=payload.get("interruptible", True),
            fallback=payload.get("fallback"),
            llm_visible=payload.get("llm_visible", True),
            api_version=payload.get("apiVersion", "asimoov/v1"),
        )


@dataclass(frozen=True)
class BehaviorCall:
    """A resolved request to run a behavior, produced by the Resolver/Executor."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    call_id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "params": self.params, "call_id": self.call_id}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BehaviorCall:
        return cls(
            name=payload["name"],
            params=payload.get("params", {}),
            call_id=payload.get("call_id") or new_id(),
        )


@dataclass(frozen=True)
class BehaviorResult:
    """Outcome returned by a `Body` primitive or the Executor.

    ``instant``/``short`` behaviors return ``ok`` (or ``error``/``timeout``)
    synchronously. ``long`` behaviors return ``started`` immediately with an
    ``action_id`` and ``eta_s``; the real outcome follows later as a mind
    injection (``[Action <id> <name>: done|failed]``), never as a second
    ``BehaviorResult``.

    Raises:
        ValueError: if ``status`` is ``error`` without a ``reason``, or
            ``started`` without an ``action_id``.
    """

    status: BehaviorStatus
    data: dict[str, Any] = field(default_factory=dict)
    action_id: str | None = None
    eta_s: float | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, BehaviorStatus):
            object.__setattr__(self, "status", BehaviorStatus(self.status))
        if self.status is BehaviorStatus.ERROR and not self.reason:
            raise ValueError("BehaviorResult(status=error) requires a reason")
        if self.status is BehaviorStatus.STARTED and not self.action_id:
            raise ValueError("BehaviorResult(status=started) requires an action_id")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status.value}
        if self.data:
            payload["data"] = self.data
        if self.action_id is not None:
            payload["action_id"] = self.action_id
        if self.eta_s is not None:
            payload["eta_s"] = self.eta_s
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BehaviorResult:
        return cls(
            status=BehaviorStatus(payload["status"]),
            data=payload.get("data", {}),
            action_id=payload.get("action_id"),
            eta_s=payload.get("eta_s"),
            reason=payload.get("reason"),
        )

    @classmethod
    def ok(cls, **data: Any) -> BehaviorResult:
        return cls(status=BehaviorStatus.OK, data=data)

    @classmethod
    def started(cls, action_id: str, eta_s: float) -> BehaviorResult:
        return cls(status=BehaviorStatus.STARTED, action_id=action_id, eta_s=eta_s)

    @classmethod
    def error(cls, reason: str) -> BehaviorResult:
        return cls(status=BehaviorStatus.ERROR, reason=reason)

    @classmethod
    def timeout(cls, reason: str = "timed out") -> BehaviorResult:
        return cls(status=BehaviorStatus.TIMEOUT, reason=reason)

    @classmethod
    def unsupported(cls, reason: str = "not supported by this body") -> BehaviorResult:
        return cls(status=BehaviorStatus.UNSUPPORTED, reason=reason)
