"""Telemetry: JSONL spans and counters, percentiles, the stats table."""

from __future__ import annotations

import json

import pytest

from asimoov.core.telemetry import (
    Telemetry,
    format_stats,
    metrics_dir,
    percentile,
    read_metrics,
    summarize,
)


def test_metrics_dir_follows_asimoov_home(asimoov_home):
    assert metrics_dir() == asimoov_home / "metrics"


def test_spans_and_counters_are_appended(tmp_path):
    telemetry = Telemetry(tmp_path)
    telemetry.record("turn", 120.0)
    telemetry.counter("barge_in")
    with telemetry.tool_span("gesture"):
        pass
    telemetry.close()

    records = read_metrics(tmp_path)
    assert {record["metric"] for record in records} == {"turn", "barge_in", "tool_latency"}
    assert [record for record in records if record["metric"] == "tool_latency"][0]["tags"] == {
        "name": "gesture"
    }


def test_a_disabled_telemetry_writes_nothing(tmp_path):
    telemetry = Telemetry(tmp_path, enabled=False)
    telemetry.record("turn", 1.0)
    assert read_metrics(tmp_path) == []


def test_turn_span_marks_its_phases(tmp_path):
    telemetry = Telemetry(tmp_path)
    turn = telemetry.turn()
    turn.mark("first_audio_delta")
    turn.mark("first_audio_played")
    turn.finish()
    telemetry.close()

    metrics = {record["metric"] for record in read_metrics(tmp_path)}
    assert metrics == {"turn.first_audio_delta", "turn.first_audio_played", "turn"}


def test_percentiles():
    values = [float(value) for value in range(1, 101)]
    assert percentile(values, 0.5) == 51.0  # nearest rank on an even sample
    assert percentile(values, 0.95) == 95.0
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_summarize_groups_spans_and_counters():
    records = [
        {"metric": "turn", "kind": "span", "ms": 100.0, "tags": {}},
        {"metric": "turn", "kind": "span", "ms": 300.0, "tags": {}},
        {"metric": "tool_latency", "kind": "span", "ms": 20.0, "tags": {"name": "gesture"}},
        {"metric": "barge_in", "kind": "counter", "count": 2, "tags": {}},
        {"metric": "barge_in", "kind": "counter", "count": 1, "tags": {}},
    ]
    summary = summarize(records)
    assert summary["turn"]["count"] == 2
    assert summary["turn"]["p95_ms"] == 300.0
    assert summary["tool_latency{gesture}"]["count"] == 1
    assert summary["barge_in"] == {"count": 3}


def test_unparsed_lines_are_counted_not_hidden(tmp_path):
    (tmp_path / "day.jsonl").write_text('{"metric":"turn","kind":"span","ms":1}\nbroken\n', encoding="utf-8")
    records = read_metrics(tmp_path)
    assert any(record["metric"] == "_unparsed" for record in records)


def test_format_stats():
    assert "no metrics" in format_stats({})
    text = format_stats({"turn": {"count": 2, "p50_ms": 1.0, "p95_ms": 2.0}, "barge_in": {"count": 4}})
    assert "turn" in text and "p95" in text and "barge_in" in text


def test_read_metrics_on_a_missing_directory(tmp_path):
    assert read_metrics(tmp_path / "nope") == []


def test_records_are_valid_json_lines(tmp_path):
    telemetry = Telemetry(tmp_path)
    telemetry.record("turn", 1.0, name="x")
    telemetry.close()
    path = next(tmp_path.glob("*.jsonl"))
    for line in path.read_text(encoding="utf-8").splitlines():
        json.loads(line)
