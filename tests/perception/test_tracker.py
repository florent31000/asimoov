"""IoU association and the 1 s hysteresis before `person_lost`."""

from __future__ import annotations

import pytest
from helpers import StubDetection

from asimoov.perception.face_id.tracker import DEFAULT_LOST_AFTER_S, IoUTracker, iou


def det(x0, y0, x1, y1, score=0.9):
    return StubDetection(bbox=(x0, y0, x1, y1), score=score, kps=None)


def test_iou_of_identical_boxes_is_one():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)


def test_iou_of_disjoint_boxes_is_zero():
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0


def test_iou_of_half_overlapping_boxes():
    assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(50 / 150)


def test_new_detection_creates_a_track():
    tracker = IoUTracker()
    update = tracker.update([det(0, 0, 50, 50)], now=0.0)
    assert len(update.visible) == 1
    assert update.lost == []
    assert update.visible[0].track_id == "t1"


def test_moving_face_keeps_its_track_id():
    tracker = IoUTracker()
    tracker.update([det(0, 0, 50, 50)], now=0.0)
    update = tracker.update([det(6, 4, 56, 54)], now=0.2)
    assert [t.track_id for t in update.visible] == ["t1"]
    assert update.visible[0].frames == 2


def test_a_jump_beyond_the_iou_threshold_starts_a_new_track():
    tracker = IoUTracker()
    tracker.update([det(0, 0, 50, 50)], now=0.0)
    update = tracker.update([det(400, 300, 450, 350)], now=0.2)
    assert [t.track_id for t in update.visible] == ["t2"]
    assert update.lost == []  # the old track is still inside the hysteresis


def test_two_faces_get_two_tracks_and_keep_them_apart():
    tracker = IoUTracker()
    tracker.update([det(0, 0, 50, 50), det(300, 0, 350, 50)], now=0.0)
    update = tracker.update([det(302, 2, 352, 52), det(2, 2, 52, 52)], now=0.2)
    by_id = {t.track_id: t.bbox for t in update.visible}
    assert by_id["t1"] == (2, 2, 52, 52)
    assert by_id["t2"] == (302, 2, 352, 52)


def test_person_lost_only_after_the_hysteresis():
    tracker = IoUTracker()
    tracker.update([det(0, 0, 50, 50)], now=0.0)
    assert tracker.update([], now=0.5).lost == []
    assert tracker.update([], now=0.99).lost == []
    lost = tracker.update([], now=DEFAULT_LOST_AFTER_S).lost
    assert [t.track_id for t in lost] == ["t1"]
    assert tracker.tracks == {}


def test_a_blink_within_the_hysteresis_does_not_break_the_track():
    tracker = IoUTracker()
    tracker.update([det(0, 0, 50, 50)], now=0.0)
    tracker.update([], now=0.4)
    update = tracker.update([det(0, 0, 50, 50)], now=0.8)
    assert [t.track_id for t in update.visible] == ["t1"]
    assert update.lost == []


def test_lost_track_is_reported_once():
    tracker = IoUTracker()
    tracker.update([det(0, 0, 50, 50)], now=0.0)
    assert len(tracker.update([], now=2.0).lost) == 1
    assert tracker.update([], now=3.0).lost == []
