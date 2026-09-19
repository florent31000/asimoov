"""Model-dependent checks, skipped when the ONNX files are not downloaded.

Only synthetic images are used: no real face is committed or needed. These
tests prove the graphs load and the shapes/normalizations are right, not
recognition accuracy.
"""

from __future__ import annotations

import numpy as np
import pytest
from helpers import synthetic_image

from asimoov.perception.models import EMBEDDING_DIM, face_models_available

pytest.importorskip("onnxruntime", reason="needs the [vision] extra")
pytest.importorskip("cv2", reason="needs the [vision] extra")
pytestmark = pytest.mark.skipif(
    not face_models_available(),
    reason="run `python tools/download_models.py` to enable the model tests",
)


def test_the_detector_finds_no_face_in_a_synthetic_image():
    from asimoov.perception.face_id.detector_scrfd import ScrfdDetector

    assert ScrfdDetector().detect(synthetic_image(640, 360)) == []


def test_the_detector_survives_odd_frame_sizes():
    from asimoov.perception.face_id.detector_scrfd import ScrfdDetector

    detector = ScrfdDetector()
    for width, height in ((320, 240), (1280, 720), (97, 61)):
        assert detector.detect(synthetic_image(width, height)) == []


def test_the_embedder_returns_a_normalized_512d_vector():
    from asimoov.perception.face_id.embedder_arcface import ArcFaceEmbedder

    vector = ArcFaceEmbedder().embed_aligned(synthetic_image(112, 112, seed=7))
    assert vector.shape == (EMBEDDING_DIM,)
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-5)


def test_the_same_crop_embeds_to_the_same_vector():
    from asimoov.perception.face_id.embedder_arcface import ArcFaceEmbedder

    embedder = ArcFaceEmbedder()
    crop = synthetic_image(112, 112, seed=11)
    assert embedder.embed_aligned(crop) == pytest.approx(embedder.embed_aligned(crop))


def test_different_crops_do_not_collapse_to_the_same_vector():
    from asimoov.perception.face_id.embedder_arcface import ArcFaceEmbedder

    embedder = ArcFaceEmbedder()
    a = embedder.embed_aligned(synthetic_image(112, 112, seed=1))
    b = embedder.embed_aligned(synthetic_image(112, 112, seed=2))
    assert float(a @ b) < 0.99
