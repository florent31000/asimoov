"""Bearing and distance derived from a face bounding box.

Sign convention (frozen, `docs/contracts.md`): `Bearing.az` is positive to the
robot's own left. A forward-facing camera maps the left of the scene to the
left of the image, so a face with a bbox centre left of the image centre
(`cx < 0.5`) has `az > 0`.
"""

from __future__ import annotations

import math

from asimoov.contracts.percepts import Bearing

#: A face taller than this fraction of the frame is "near", taller than
#: MEDIUM is "medium", anything smaller is "far".
NEAR_HEIGHT_RATIO = 0.33
MEDIUM_HEIGHT_RATIO = 0.13


def bearing_from_bbox(
    bbox_norm: tuple[float, float, float, float],
    *,
    fov_h_deg: float,
    aspect: float,
) -> Bearing:
    """Pinhole bearing of a normalized bbox `(x0, y0, x1, y1)` in `[0, 1]`.

    `aspect` is the frame's height / width ratio, used to derive the vertical
    field of view from the configured horizontal one.
    """
    x0, y0, x1, y1 = bbox_norm
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    half_width = math.tan(math.radians(fov_h_deg) / 2.0)
    x_off = (0.5 - cx) * 2.0 * half_width
    y_off = (0.5 - cy) * 2.0 * half_width * aspect
    return Bearing(az=math.degrees(math.atan(x_off)), el=math.degrees(math.atan(y_off)))


def distance_class_from_bbox(bbox_norm: tuple[float, float, float, float]) -> str:
    """Coarse distance class from the face height relative to the frame."""
    height = bbox_norm[3] - bbox_norm[1]
    if height >= NEAR_HEIGHT_RATIO:
        return "near"
    if height >= MEDIUM_HEIGHT_RATIO:
        return "medium"
    return "far"
