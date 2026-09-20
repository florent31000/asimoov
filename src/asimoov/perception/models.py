"""ONNX model registry: download, verify, and locate the perception models.

Models live in `$ASIMOOV_HOME/models/` (`~/.asimoov/models/` by default) and
are never committed. `download_models` is the function
`asimoov doctor --download-models` (WS1) calls. The registry also carries
Kokoro's two files (extra `[claude]`, WS2's `voice/claude_pipeline/tts.py`):
they live under the same `models_dir()`, so one command fetches everything
a robot needs on disk.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from asimoov.core.config import asimoov_home
from asimoov.perception import PerceptionError

BUFFALO_SC_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_sc.zip"
BUFFALO_SC_SHA256 = "57d31b56b6ffa911c8a73cfc1707c73cab76efe7f13b675a05223bf42de47c72"
SILERO_VAD_URL = (
    "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"
)
KOKORO_RELEASE_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
)


@dataclass(frozen=True)
class ModelSpec:
    """One ONNX file: where it comes from and what it must hash to."""

    name: str
    filename: str
    sha256: str
    url: str
    archive_member: str | None = None
    optional: bool = False


DETECTOR = ModelSpec(
    name="scrfd_500m",
    filename="det_500m.onnx",
    sha256="5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a",
    url=BUFFALO_SC_URL,
    archive_member="det_500m.onnx",
)
EMBEDDER = ModelSpec(
    name="mobilefacenet_w600k",
    filename="w600k_mbf.onnx",
    sha256="9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f",
    url=BUFFALO_SC_URL,
    archive_member="w600k_mbf.onnx",
)
SILERO_VAD = ModelSpec(
    name="silero_vad",
    filename="silero_vad.onnx",
    sha256="2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f",
    url=SILERO_VAD_URL,
    optional=True,
)
#: Kokoro TTS (extra `[claude]`); filenames match
#: `voice.claude_pipeline.tts.MODEL_FILENAME` / `VOICES_FILENAME`, which look
#: for them at the same `models_dir()`.
KOKORO_MODEL = ModelSpec(
    name="kokoro_v1",
    filename="kokoro-v1.0.onnx",
    sha256="beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",
    url=f"{KOKORO_RELEASE_URL}/kokoro-v1.0.onnx",
    optional=True,
)
KOKORO_VOICES = ModelSpec(
    name="kokoro_voices_v1",
    filename="voices-v1.0.bin",
    sha256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
    url=f"{KOKORO_RELEASE_URL}/voices-v1.0.bin",
    optional=True,
)

MODELS: tuple[ModelSpec, ...] = (DETECTOR, EMBEDDER, SILERO_VAD, KOKORO_MODEL, KOKORO_VOICES)

#: Embedding model identity stored next to every vector in `face_embeddings`.
EMBEDDING_MODEL_ID = "w600k_mbf"
EMBEDDING_DIM = 512


def models_dir() -> Path:
    """Directory holding the downloaded ONNX files (`ASIMOOV_MODELS_DIR` wins)."""
    override = os.environ.get("ASIMOOV_MODELS_DIR")
    if override:
        return Path(override)
    return asimoov_home() / "models"


def model_path(spec: ModelSpec) -> Path:
    return models_dir() / spec.filename


def is_present(spec: ModelSpec) -> bool:
    return model_path(spec).is_file()


def face_models_available() -> bool:
    """True when both face models are on disk (used by `skipif` in tests)."""
    return is_present(DETECTOR) and is_present(EMBEDDER)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(spec: ModelSpec) -> Path:
    """Return the local path of a model, or explain how to get it.

    Raises:
        PerceptionError: if the file is missing.
    """
    path = model_path(spec)
    if not path.is_file():
        raise PerceptionError(
            f"model {spec.name} missing at {path}; run `asimoov doctor --download-models`"
        )
    return path


def _verify(path: Path, spec: ModelSpec) -> None:
    actual = sha256_of(path)
    if actual != spec.sha256:
        path.unlink(missing_ok=True)
        raise PerceptionError(
            f"checksum mismatch for {spec.filename}: expected {spec.sha256}, got {actual}"
        )


def _fetch(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    tmp.replace(destination)


def download_models(*, include_optional: bool = True, force: bool = False) -> list[Path]:
    """Download and verify every model, returning the local paths.

    The hook behind `asimoov doctor --download-models`. Already-present files
    with a matching checksum are left alone.

    Raises:
        PerceptionError: on a checksum mismatch after download.
    """
    wanted = [s for s in MODELS if include_optional or not s.optional]
    archives: dict[str, Path] = {}
    paths: list[Path] = []
    for spec in wanted:
        destination = model_path(spec)
        if destination.is_file() and not force:
            _verify(destination, spec)
            paths.append(destination)
            continue
        if spec.archive_member is None:
            _fetch(spec.url, destination)
        else:
            archive = archives.get(spec.url)
            if archive is None:
                archive = models_dir() / Path(spec.url).name
                if not archive.is_file() or force:
                    _fetch(spec.url, archive)
                archives[spec.url] = archive
            with zipfile.ZipFile(archive) as bundle:
                destination.write_bytes(bundle.read(spec.archive_member))
        _verify(destination, spec)
        paths.append(destination)
    for archive in archives.values():
        archive.unlink(missing_ok=True)
    return paths
