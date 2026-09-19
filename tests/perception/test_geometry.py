"""Bearing and distance maths, including the frozen sign convention."""

from __future__ import annotations

import pytest

from asimoov.perception.geometry import bearing_from_bbox, distance_class_from_bbox

FOV = 70.0
ASPECT = 360 / 640


def bbox_centered_at(cx: float, cy: float, size: float = 0.1):
    return (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2)


def test_centre_of_frame_is_straight_ahead():
    bearing = bearing_from_bbox(bbox_centered_at(0.5, 0.5), fov_h_deg=FOV, aspect=ASPECT)
    assert bearing.az == pytest.approx(0.0, abs=1e-9)
    assert bearing.el == pytest.approx(0.0, abs=1e-9)


def test_face_on_the_left_of_the_image_is_on_the_robots_left():
    # Frozen convention (docs/contracts.md): az > 0 = the robot's own left,
    # and a forward-facing camera maps the scene's left to the image's left.
    bearing = bearing_from_bbox(bbox_centered_at(0.2, 0.5), fov_h_deg=FOV, aspect=ASPECT)
    assert bearing.az > 0


def test_face_on_the_right_of_the_image_is_negative():
    bearing = bearing_from_bbox(bbox_centered_at(0.8, 0.5), fov_h_deg=FOV, aspect=ASPECT)
    assert bearing.az < 0


def test_azimuth_is_symmetric_around_the_centre():
    left = bearing_from_bbox(bbox_centered_at(0.25, 0.5), fov_h_deg=FOV, aspect=ASPECT)
    right = bearing_from_bbox(bbox_centered_at(0.75, 0.5), fov_h_deg=FOV, aspect=ASPECT)
    assert left.az == pytest.approx(-right.az)


def test_frame_edges_are_half_the_field_of_view():
    left_edge = bearing_from_bbox((0.0, 0.4, 0.0, 0.6), fov_h_deg=FOV, aspect=ASPECT)
    assert left_edge.az == pytest.approx(FOV / 2, abs=1e-6)


def test_elevation_is_positive_above_the_centre():
    bearing = bearing_from_bbox(bbox_centered_at(0.5, 0.2), fov_h_deg=FOV, aspect=ASPECT)
    assert bearing.el > 0
    assert abs(bearing.el) < FOV / 2  # narrower vertical field of view


def test_azimuth_grows_with_the_field_of_view():
    narrow = bearing_from_bbox(bbox_centered_at(0.2, 0.5), fov_h_deg=40.0, aspect=ASPECT)
    wide = bearing_from_bbox(bbox_centered_at(0.2, 0.5), fov_h_deg=100.0, aspect=ASPECT)
    assert wide.az > narrow.az


@pytest.mark.parametrize(
    ("height", "expected"),
    [(0.5, "near"), (0.35, "near"), (0.2, "medium"), (0.13, "medium"), (0.05, "far")],
)
def test_distance_class_from_face_height(height: float, expected: str):
    assert distance_class_from_bbox((0.4, 0.1, 0.6, 0.1 + height)) == expected
