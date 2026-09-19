"""The model registry. Nothing here downloads anything."""

from __future__ import annotations

import hashlib

import pytest

from asimoov.perception import PerceptionError
from asimoov.perception.models import (
    DETECTOR,
    EMBEDDER,
    EMBEDDING_DIM,
    MODELS,
    SILERO_VAD,
    face_models_available,
    is_present,
    model_path,
    models_dir,
    require,
    sha256_of,
)


def test_every_model_declares_a_checksum_and_a_url():
    for spec in MODELS:
        assert len(spec.sha256) == 64
        assert spec.url.startswith("https://")
        assert spec.filename.endswith(".onnx")


def test_the_face_models_come_from_the_same_buffalo_pack():
    assert DETECTOR.url == EMBEDDER.url
    assert DETECTOR.archive_member == "det_500m.onnx"
    assert EMBEDDER.archive_member == "w600k_mbf.onnx"
    assert EMBEDDING_DIM == 512


def test_only_the_vad_model_is_optional():
    assert [spec.name for spec in MODELS if spec.optional] == [SILERO_VAD.name]


def test_the_models_directory_follows_asimoov_home(monkeypatch, tmp_path):
    monkeypatch.delenv("ASIMOOV_MODELS_DIR", raising=False)
    monkeypatch.setenv("ASIMOOV_HOME", str(tmp_path))
    assert models_dir() == tmp_path / "models"
    assert model_path(SILERO_VAD) == tmp_path / "models" / "silero_vad.onnx"


def test_the_models_directory_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("ASIMOOV_MODELS_DIR", str(tmp_path))
    assert models_dir() == tmp_path
    assert model_path(DETECTOR) == tmp_path / "det_500m.onnx"
    assert is_present(DETECTOR) is False
    assert face_models_available() is False


def test_a_missing_model_explains_how_to_get_it(monkeypatch, tmp_path):
    monkeypatch.setenv("ASIMOOV_MODELS_DIR", str(tmp_path))
    with pytest.raises(PerceptionError, match="doctor --download-models"):
        require(DETECTOR)


def test_sha256_of_a_local_file(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"asimoov")
    assert sha256_of(path) == hashlib.sha256(b"asimoov").hexdigest()
