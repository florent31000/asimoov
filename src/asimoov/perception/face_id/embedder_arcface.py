"""MobileFaceNet w600k embedder (InsightFace ``buffalo_sc``) via onnxruntime.

Takes the 5 SCRFD keypoints, aligns the face to the 112x112 ArcFace template
with a similarity transform, and returns a 512-d L2-normalized vector. The
crop exists only in memory for the duration of the call.
"""

from __future__ import annotations

import numpy as np

from asimoov.perception import PerceptionError
from asimoov.perception.models import EMBEDDER, EMBEDDING_DIM, require

#: ArcFace 112x112 reference landmarks (eyes, nose, mouth corners).
ARCFACE_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)
ALIGNED_SIZE = 112

#: A face crop whose Laplacian variance reaches this is considered sharp.
SHARPNESS_REFERENCE = 120.0
#: A face this tall in the source image (pixels) is considered full resolution.
RESOLUTION_REFERENCE_PX = 96.0


def similarity_transform(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform (Umeyama) mapping ``src`` to ``dst``.

    Returns the 2x3 affine matrix ``cv2.warpAffine`` expects.

    Raises:
        ValueError: if fewer than two point pairs are given.
    """
    if src.shape[0] < 2 or src.shape != dst.shape:
        raise ValueError(f"need matching point sets of size >= 2, got {src.shape} / {dst.shape}")
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_centered = src - src_mean
    dst_centered = dst - dst_mean
    covariance = dst_centered.T @ src_centered / src.shape[0]
    u, singular, vt = np.linalg.svd(covariance)
    correction = np.eye(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        correction[1, 1] = -1
    rotation = u @ correction @ vt
    variance = src_centered.var(axis=0).sum()
    scale = 1.0 if variance == 0 else (singular * np.diag(correction)).sum() / variance
    matrix = np.zeros((2, 3), dtype=np.float32)
    matrix[:, :2] = scale * rotation
    matrix[:, 2] = dst_mean - scale * rotation @ src_mean
    return matrix


def align_face(image_bgr: np.ndarray, kps: np.ndarray) -> np.ndarray:
    """Warp the face onto the 112x112 ArcFace template."""
    import cv2

    matrix = similarity_transform(np.asarray(kps, dtype=np.float32), ARCFACE_TEMPLATE)
    return cv2.warpAffine(image_bgr, matrix, (ALIGNED_SIZE, ALIGNED_SIZE), borderValue=0)


def face_quality(aligned_bgr: np.ndarray, *, det_score: float, bbox_height_px: float) -> float:
    """Score a face in ``[0, 1]``: detector confidence x resolution x sharpness.

    Enrolment keeps only samples above 0.6, which in practice means a sharp,
    confidently detected face at least ~60 px tall.
    """
    import cv2

    resolution = min(1.0, bbox_height_px / RESOLUTION_REFERENCE_PX)
    grey = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2GRAY)
    sharpness = min(1.0, float(cv2.Laplacian(grey, cv2.CV_64F).var()) / SHARPNESS_REFERENCE)
    return float(max(0.0, min(1.0, det_score)) * resolution * sharpness)


class ArcFaceEmbedder:
    """512-d face embeddings from an aligned 112x112 crop."""

    dim = EMBEDDING_DIM

    def __init__(self, *, providers: list[str] | None = None) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - depends on the [vision] extra
            raise PerceptionError("face embedding needs `pip install asimoov[vision]`") from exc
        self._session = ort.InferenceSession(
            str(require(EMBEDDER)), providers=providers or ["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        self._output_name = self._session.get_outputs()[0].name

    def embed_aligned(self, aligned_bgr: np.ndarray) -> np.ndarray:
        """Embed an already-aligned 112x112 BGR crop."""
        import cv2

        blob = cv2.dnn.blobFromImage(
            aligned_bgr,
            1.0 / 127.5,
            (ALIGNED_SIZE, ALIGNED_SIZE),
            (127.5, 127.5, 127.5),
            swapRB=True,
        )
        vector = self._session.run([self._output_name], {self._input_name: blob})[0][0]
        return normalize(vector)

    def embed(self, image_bgr: np.ndarray, kps: np.ndarray) -> np.ndarray:
        """Align with the 5 keypoints, then embed."""
        return self.embed_aligned(align_face(image_bgr, kps))


def normalize(vector: np.ndarray) -> np.ndarray:
    """L2-normalize, so cosine similarity is a plain dot product."""
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        raise ValueError("cannot normalize a zero embedding")
    return vector / norm
