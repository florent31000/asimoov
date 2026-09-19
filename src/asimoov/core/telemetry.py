"""Latency spans and counters, appended as JSONL under `~/.asimoov/metrics/`.

Neon shipped with no telemetry at all (plan.md section 2, item 10), so
latency drift was invisible. The spans that matter are named in plan.md
section 4.11: ``turn`` (speech stopped -> first audio delta -> first audio
actually played), ``tool_latency`` per tool, plus the ``barge_in``,
``injection`` and ``session_renewal`` counters. `asimoov stats` reads the
files back and prints p50/p95.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from asimoov.core.config import asimoov_home

TURN_SPAN = "turn"
TOOL_LATENCY_SPAN = "tool_latency"
TURN_FIRST_DELTA = "turn.first_audio_delta"
TURN_FIRST_PLAYED = "turn.first_audio_played"


def metrics_dir() -> Path:
    return asimoov_home() / "metrics"


@dataclass
class TurnSpan:
    """A conversation turn, measured in phases from the end of user speech."""

    telemetry: Telemetry
    started_at: float
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, phase: str) -> float:
        """Record ``phase`` (e.g. ``first_audio_delta``) and return its offset in ms."""
        elapsed_ms = (time.monotonic() - self.started_at) * 1000
        self.marks[phase] = elapsed_ms
        self.telemetry.record(f"{TURN_SPAN}.{phase}", elapsed_ms)
        return elapsed_ms

    def finish(self) -> float:
        elapsed_ms = (time.monotonic() - self.started_at) * 1000
        self.telemetry.record(TURN_SPAN, elapsed_ms)
        return elapsed_ms


class Telemetry:
    """Appends spans and counters to a daily JSONL file.

    ``enabled=False`` keeps the API usable (tests, ``--no-telemetry``)
    without touching the filesystem.
    """

    def __init__(self, directory: Path | None = None, *, enabled: bool = True) -> None:
        self.directory = directory or metrics_dir()
        self.enabled = enabled
        self._handle = None

    def _file(self):
        if self._handle is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d", time.localtime())
            self._handle = (self.directory / f"{day}.jsonl").open("a", encoding="utf-8")
        return self._handle

    def _write(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        handle = self._file()
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        handle.flush()

    def record(self, metric: str, duration_ms: float, **tags: Any) -> None:
        """Record a completed span duration in milliseconds."""
        self._write(
            {"ts": time.time(), "metric": metric, "kind": "span", "ms": duration_ms, "tags": tags}
        )

    def counter(self, metric: str, value: int = 1, **tags: Any) -> None:
        """Increment a counter by ``value``."""
        self._write(
            {"ts": time.time(), "metric": metric, "kind": "counter", "count": value, "tags": tags}
        )

    @contextmanager
    def span(self, metric: str, **tags: Any) -> Iterator[None]:
        """Time the wrapped block and record it as ``metric``."""
        started = time.monotonic()
        try:
            yield
        finally:
            self.record(metric, (time.monotonic() - started) * 1000, **tags)

    def tool_span(self, name: str):
        """Time a tool call, tagged with the tool name."""
        return self.span(TOOL_LATENCY_SPAN, name=name)

    def turn(self) -> TurnSpan:
        """Start a ``turn`` span at the end of user speech."""
        return TurnSpan(telemetry=self, started_at=time.monotonic())

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def read_metrics(directory: Path | None = None) -> list[dict[str, Any]]:
    """Read every metric record from the JSONL files in ``directory``.

    Malformed lines are skipped, but the count of skipped lines is not
    hidden: it is returned as a record with metric ``_unparsed``.
    """
    directory = directory or metrics_dir()
    records: list[dict[str, Any]] = []
    unparsed = 0
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                unparsed += 1
    if unparsed:
        records.append({"metric": "_unparsed", "kind": "counter", "count": unparsed, "tags": {}})
    return records


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of ``values`` (``fraction`` in [0, 1])."""
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def summarize(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group records by metric (and tool name) into count/p50/p95 summaries."""
    spans: dict[str, list[float]] = {}
    counters: dict[str, int] = {}
    for record in records:
        name = record.get("metric", "")
        tag_name = (record.get("tags") or {}).get("name")
        if tag_name:
            name = f"{name}{{{tag_name}}}"
        if record.get("kind") == "span":
            spans.setdefault(name, []).append(float(record.get("ms", 0.0)))
        else:
            counters[name] = counters.get(name, 0) + int(record.get("count", 1))

    summary: dict[str, dict[str, Any]] = {}
    for name, values in sorted(spans.items()):
        summary[name] = {
            "count": len(values),
            "p50_ms": round(percentile(values, 0.50), 1),
            "p95_ms": round(percentile(values, 0.95), 1),
        }
    for name, total in sorted(counters.items()):
        summary[name] = {"count": total}
    return summary


def format_stats(summary: dict[str, dict[str, Any]]) -> str:
    """Render a summary as the table `asimoov stats` prints."""
    if not summary:
        return "no metrics recorded yet"
    width = max(len(name) for name in summary)
    lines = []
    for name, values in summary.items():
        if "p50_ms" in values:
            lines.append(
                f"{name:<{width}}  n={values['count']:<5} p50={values['p50_ms']:>8.1f} ms"
                f"  p95={values['p95_ms']:>8.1f} ms"
            )
        else:
            lines.append(f"{name:<{width}}  n={values['count']}")
    return "\n".join(lines)
