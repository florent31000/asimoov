"""Face alignment maths and the quality score (no model needed)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2", reason="needs the [vision] extra")

from helpers import synthetic_image  # noqa: E402

from asimoov.perception.face_id.embedder_arcface import (  # noqa: E402
    ALIGNED_SIZE,
    ARCFACE_TEMPLATE,
    align_face,
    face_quality,
    normalize,
    similarity_transform,
)


def apply(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    return points @ matrix[:, :2].T + matrix[:, 2]


def test_identity_transform():
    matrix = similarity_transform(ARCFACE_TEMPLATE, ARCFACE_TEMPLATE)
    assert apply(matrix, ARCFACE_TEMPLATE) == pytest.approx(ARCFACE_TEMPLATE, abs=1e-3)


def test_a_scaled_and_shifted_face_maps_back_onto_the_template():
    source = ARCFACE_TEMPLATE * 2.0 + np.array([30.0, -10.0], dtype=np.float32)
    matrix = similarity_transform(source, ARCFACE_TEMPLATE)
    assert apply(matrix, source) == pytest.approx(ARCFACE_TEMPLATE, abs=1e-3)


def test_a_rotated_face_maps_back_onto_the_template():
    angle = np.deg2rad(20.0)
    rotation = np.array(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]], dtype=np.float32
    )
    source = ARCFACE_TEMPLATE @ rotation.T
    matrix = similarity_transform(source, ARCFACE_TEMPLATE)
    assert apply(matrix, source) == pytest.approx(ARCFACE_TEMPLATE, abs=1e-3)


def test_too_few_points_is_an_error():
    with pytest.raises(ValueError, match="size >= 2"):
        similarity_transform(ARCFACE_TEMPLATE[:1], ARCFACE_TEMPLATE[:1])


def test_alignment_produces_a_112_square():
    aligned = align_face(synthetic_image(640, 360), ARCFACE_TEMPLATE * 1.5)
    assert aligned.shape == (ALIGNED_SIZE, ALIGNED_SIZE, 3)


def test_quality_rises_with_detector_confidence():
    crop = synthetic_image(112, 112, seed=5)
    low = face_quality(crop, det_score=0.4, bbox_height_px=120)
    high = face_quality(crop, det_score=0.95, bbox_height_px=120)
    assert 0.0 <= low < high <= 1.0


def test_a_small_face_scores_lower_than_a_large_one():
    crop = synthetic_image(112, 112, seed=5)
    far = face_quality(crop, det_score=0.95, bbox_height_px=30)
    near = face_quality(crop, det_score=0.95, bbox_height_px=120)
    assert far < near


def test_a_flat_crop_has_no_sharpness_and_scores_zero():
    flat = np.full((112, 112, 3), 128, dtype=np.uint8)
    assert face_quality(flat, det_score=1.0, bbox_height_px=200) == 0.0


def test_normalize_rejects_a_zero_vector():
    with pytest.raises(ValueError, match="zero embedding"):
        normalize(np.zeros(8, dtype=np.float32))


def test_normalize_returns_a_unit_vector():
    vector = normalize(np.array([3.0, 4.0], dtype=np.float32))
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0)
