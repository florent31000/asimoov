"""`ToolRegistry`: the tools the model sees, generated from what the body can do.

Enums are dynamic (plan.md section 4.3): the ``gesture`` tool only lists
behaviors this body can honour, and ``set_expression`` only the persona's
emotions. A tool the robot cannot perform is never offered, so the model
cannot promise it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from asimoov.contracts.behaviors import BehaviorCall, BehaviorStatus
from asimoov.contracts.body import GazeTarget
from asimoov.contracts.tools import ToolHandler, ToolResult, ToolSpec
from asimoov.core.behaviors.executor import BehaviorExecutor
from asimoov.core.telemetry import Telemetry
from asimoov.core.timeouts import run_with_timeout

log = logging.getLogger(__name__)

STRUCTURAL_BEHAVIORS = ("move", "turn", "look_at", "express")
TOOL_NAME_FOR_BEHAVIOR = {"express": "set_expression"}
GESTURE_TOOL_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    """Holds the tool specs and runs the calls under their own timeout."""

    def __init__(self, *, telemetry: Telemetry | None = None) -> None:
        self.telemetry = telemetry
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler, *, replace: bool = False) -> None:
        """Register ``spec``.

        Raises:
            ValueError: if the name is already taken and ``replace`` is False.
        """
        if spec.name in self._tools and not replace:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = RegisteredTool(spec=spec, handler=handler)

    def register_all(self, tools, *, replace: bool = False) -> None:
        for spec, handler in tools:
            self.register(spec, handler, replace=replace)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def clear(self) -> None:
        """Forget every tool.

        The runtime rebuilds the whole list when a body republishes its
        manifest: a gesture the bust no longer has must disappear, not just
        be overwritten by a namesake.
        """
        self._tools.clear()

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        """Every tool spec, in registration order: what the provider advertises."""
        return tuple(tool.spec for tool in self._tools.values())

    async def call(self, name: str, params: dict[str, Any]) -> ToolResult:
        """Run a tool call, always returning a `ToolResult` (never raising)."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(status=BehaviorStatus.ERROR, reason=f"unknown tool {name!r}")
        try:
            if self.telemetry is not None:
                with self.telemetry.tool_span(name):
                    answered, result = await run_with_timeout(
                        tool.handler(params), tool.spec.timeout_s
                    )
            else:
                answered, result = await run_with_timeout(
                    tool.handler(params), tool.spec.timeout_s
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("tool %s raised", name)
            return ToolResult(status=BehaviorStatus.ERROR, reason=f"{name} failed: {exc}")
        if not answered:
            return ToolResult(
                status=BehaviorStatus.TIMEOUT,
                reason=f"{name} did not answer within {tool.spec.timeout_s}s",
            )
        return result


def build_behavior_tools(
    resolver,
    executor: BehaviorExecutor,
    *,
    emotions: tuple[str, ...] = (),
    scene_provider=None,
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Generate the LLM tools for whatever behaviors this body can run."""
    visible = {manifest.name: manifest for manifest in resolver.visible()}
    tools: list[tuple[ToolSpec, ToolHandler]] = []

    gesture_names = sorted(
        name for name in visible if name not in STRUCTURAL_BEHAVIORS and name != "stop"
    )
    if gesture_names:
        spec = ToolSpec(
            name="gesture",
            description="Perform a physical gesture. Only the listed gestures exist on this body.",
            params={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": gesture_names},
                    "person": {"type": "string", "description": "name or 'current'"},
                },
                "required": ["name"],
            },
            timeout_s=GESTURE_TOOL_TIMEOUT_S,
            duration_class="long",
        )

        async def run_gesture(params: dict[str, Any]) -> ToolResult:
            name = params.get("name", "")
            if name not in gesture_names:
                return ToolResult(status=BehaviorStatus.ERROR, reason=f"unknown gesture {name!r}")
            call_params = {key: value for key, value in params.items() if key != "name"}
            result = await executor.run(BehaviorCall(name=name, params=call_params))
            return ToolResult.from_behavior_result(result)

        tools.append((spec, run_gesture))

    for behavior_name in STRUCTURAL_BEHAVIORS:
        manifest = visible.get(behavior_name)
        if manifest is None:
            continue
        tool_name = TOOL_NAME_FOR_BEHAVIOR.get(behavior_name, behavior_name)
        params = dict(manifest.params)
        if behavior_name == "express" and emotions:
            params = {
                "type": "object",
                "properties": {
                    "emotion": {"type": "string", "enum": list(emotions)},
                    "intensity": {"type": "number", "description": "0 to 1"},
                },
                "required": ["emotion"],
            }
        spec = ToolSpec(
            name=tool_name,
            description=manifest.description,
            params=params,
            timeout_s=GESTURE_TOOL_TIMEOUT_S,
            duration_class=manifest.duration_class,
        )
        tools.append(
            (spec, _behavior_handler(behavior_name, executor, scene_provider=scene_provider))
        )
    return tools


def _behavior_handler(behavior_name: str, executor: BehaviorExecutor, *, scene_provider=None):
    async def handler(params: dict[str, Any]) -> ToolResult:
        call_params = dict(params)
        if behavior_name == "look_at":
            resolved = _resolve_gaze(call_params.pop("target", "ahead"), scene_provider)
            if resolved is None:
                return ToolResult(
                    status=BehaviorStatus.ERROR,
                    reason=f"nobody called {params.get('target')!r} is here",
                )
            call_params.setdefault("az", resolved.az)
            call_params.setdefault("el", resolved.el)
            if resolved.track_id:
                call_params.setdefault("track_id", resolved.track_id)
        result = await executor.run(BehaviorCall(name=behavior_name, params=call_params))
        return ToolResult.from_behavior_result(result)

    return handler


def _resolve_gaze(target: Any, scene_provider) -> GazeTarget | None:
    """Turn a name, a track id or ``ahead`` into a bearing."""
    if not isinstance(target, str) or target in ("ahead", "", "front"):
        return GazeTarget(az=0.0, el=0.0)
    scene = scene_provider() if scene_provider is not None else None
    if scene is None:
        return None
    for person in scene.people:
        if person.track_id == target or (person.name and person.name.lower() == target.lower()):
            return GazeTarget(
                az=person.bearing.az, el=person.bearing.el, track_id=person.track_id
            )
    if target == "current" and scene.attention() is not None:
        person = scene.attention()
        return GazeTarget(az=person.bearing.az, el=person.bearing.el, track_id=person.track_id)
    return None
