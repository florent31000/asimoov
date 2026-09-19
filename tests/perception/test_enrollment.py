"""The enrolment state machine: 5 samples above 0.6, within 10 s."""

from __future__ import annotations

import numpy as np
import pytest

from asimoov.perception.face_id.enroll import (
    REASON_CANCELLED,
    REASON_TIMEOUT,
    REASON_TRACK_LOST,
    Enrollment,
)


def make(**kwargs) -> Enrollment:
    defaults = {"track_id": "t1", "person_id": "p_sam", "started_at": 0.0}
    return Enrollment(**{**defaults, **kwargs})


def vec(value: float = 1.0) -> np.ndarray:
    return np.array([value, 0.0], dtype=np.float32)


def test_five_good_samples_succeed():
    enrollment = make()
    for index in range(5):
        assert enrollment.offer("t1", vec(), 0.8, now=index * 0.2)
    result = enrollment.tick(1.0)
    assert result.ok
    assert result.samples == 5
    assert result.person_id == "p_sam"
    assert result.to_payload() == {
        "ok": True,
        "samples": 5,
        "person_id": "p_sam",
        "track_id": "t1",
    }


def test_samples_at_or_below_the_quality_threshold_are_rejected():
    enrollment = make()
    assert not enrollment.offer("t1", vec(), 0.6, now=0.1)
    assert not enrollment.offer("t1", vec(), 0.2, now=0.2)
    assert enrollment.collected == []
    assert enrollment.best_quality == pytest.approx(0.6)


def test_samples_from_another_track_are_ignored():
    enrollment = make()
    assert not enrollment.offer("t2", vec(), 0.9, now=0.1)
    assert enrollment.collected == []


def test_timeout_reports_the_partial_count_and_the_best_quality():
    enrollment = make()
    enrollment.offer("t1", vec(), 0.65, now=1.0)
    assert enrollment.tick(9.9) is None
    result = enrollment.tick(10.0)
    assert not result.ok
    assert result.reason == REASON_TIMEOUT
    assert result.samples == 1
    assert result.to_payload()["best_quality"] == pytest.approx(0.65)


def test_losing_the_track_fails_immediately():
    enrollment = make()
    enrollment.offer("t1", vec(), 0.9, now=0.1)
    result = enrollment.track_lost()
    assert not result.ok
    assert result.reason == REASON_TRACK_LOST
    assert result.samples == 1


def test_cancelling_fails_with_its_own_reason():
    assert make().cancel().reason == REASON_CANCELLED


def test_a_finished_enrolment_is_final():
    enrollment = make()
    for index in range(5):
        enrollment.offer("t1", vec(), 0.9, now=index * 0.1)
    assert enrollment.done
    assert not enrollment.offer("t1", vec(), 0.99, now=1.0)
    assert enrollment.track_lost().ok  # already succeeded, not overwritten
    assert enrollment.tick(100.0).ok


def test_a_shorter_sample_target_is_honoured():
    enrollment = make(samples=2)
    enrollment.offer("t1", vec(), 0.7, now=0.0)
    enrollment.offer("t1", vec(), 0.7, now=0.2)
    assert enrollment.done
    assert enrollment.finished.samples == 2
