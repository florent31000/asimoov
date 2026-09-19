"""SCRFD-500M face detector (InsightFace ``buffalo_sc`` pack) via onnxruntime.

Outputs, per stride 8/16/32 and 2 anchors per cell: a score, a
distance-to-box regression, and 5 keypoints. Post-processing follows the
reference SCRFD decoding; the model file itself is downloaded by
``perception.models``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from asimoov.perception import PerceptionError
from asimoov.perception.models import DETECTOR, require

STRIDES: tuple[int, ...] = (8, 16, 32)
ANCHORS_PER_CELL = 2
DEFAULT_INPUT_SIZE = (640, 640)
DEFAULT_SCORE_THRESHOLD = 0.5
DEFAULT_NMS_THRESHOLD = 0.4


@dataclass(frozen=True)
class Detection:
    """One detected face in the source image's pixel coordinates."""

    bbox: tuple[float, float, float, float]
    score: float
    kps: Any  # (5, 2) float32 array: eyes, nose, mouth corners


def _distance2points(centers: np.ndarray, distances: np.ndarray) -> np.ndarray:
    x1 = centers[:, 0] - distances[:, 0]
    y1 = centers[:, 1] - distances[:, 1]
    x2 = centers[:, 0] + distances[:, 2]
    y2 = centers[:, 1] + distances[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(centers: np.ndarray, distances: np.ndarray) -> np.ndarray:
    points = distances.reshape(distances.shape[0], -1, 2).copy()
    points[:, :, 0] += centers[:, 0:1]
    points[:, :, 1] += centers[:, 1:2]
    return points


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    """Greedy IoU non-maximum suppression, returning the kept indices."""
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        best = int(order[0])
        keep.append(best)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[best], x1[rest])
        yy1 = np.maximum(y1[best], y1[rest])
        xx2 = np.minimum(x2[best], x2[rest])
        yy2 = np.minimum(y2[best], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / np.maximum(areas[best] + areas[rest] - inter, 1e-9)
        order = rest[iou <= threshold]
    return keep


class ScrfdDetector:
    """Detect faces in a BGR image. Thread-confined: one instance per loop."""

    def __init__(
        self,
        *,
        input_size: tuple[int, int] = DEFAULT_INPUT_SIZE,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        nms_threshold: float = DEFAULT_NMS_THRESHOLD,
        providers: list[str] | None = None,
    ) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - depends on the [vision] extra
            raise PerceptionError("face detection needs `pip install asimoov[vision]`") from exc
        options = ort.SessionOptions()
        options.intra_op_num_threads = 0
        self._session = ort.InferenceSession(
            str(require(DETECTOR)),
            sess_options=options,
            providers=providers or ["CPUExecutionProvider"],
        )
        self._input_name = self._session.get_inputs()[0].name
        self._output_names = [o.name for o in self._session.get_outputs()]
        self.input_size = input_size
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self._centers: dict[tuple[int, int, int], np.ndarray] = {}

    def _anchor_centers(self, height: int, width: int, stride: int) -> np.ndarray:
        key = (height, width, stride)
        cached = self._centers.get(key)
        if cached is not None:
            return cached
        rows, cols = height // stride, width // stride
        grid_y, grid_x = np.mgrid[:rows, :cols]
        centers = np.stack([grid_x, grid_y], axis=-1).astype(np.float32) * stride
        centers = np.repeat(centers.reshape(-1, 2), ANCHORS_PER_CELL, axis=0)
        self._centers[key] = centers
        return centers

    def detect(self, image_bgr: np.ndarray) -> list[Detection]:
        """Return the detected faces, largest score first."""
        import cv2

        target_w, target_h = self.input_size
        src_h, src_w = image_bgr.shape[:2]
        scale = min(target_w / src_w, target_h / src_h)
        resized = cv2.resize(image_bgr, (int(round(src_w * scale)), int(round(src_h * scale))))
        canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        canvas[: resized.shape[0], : resized.shape[1]] = resized
        blob = cv2.dnn.blobFromImage(
            canvas, 1.0 / 128.0, (target_w, target_h), (127.5, 127.5, 127.5), swapRB=True
        )
        outputs = self._session.run(self._output_names, {self._input_name: blob})

        boxes: list[np.ndarray] = []
        scores: list[np.ndarray] = []
        keypoints: list[np.ndarray] = []
        levels = len(STRIDES)
        for index, stride in enumerate(STRIDES):
            level_scores = outputs[index].reshape(-1)
            keep = np.where(level_scores >= self.score_threshold)[0]
            if keep.size == 0:
                continue
            centers = self._anchor_centers(target_h, target_w, stride)[keep]
            level_boxes = _distance2points(centers, outputs[index + levels].reshape(-1, 4)[keep] * stride)
            level_kps = _distance2kps(centers, outputs[index + 2 * levels].reshape(-1, 10)[keep] * stride)
            boxes.append(level_boxes)
            scores.append(level_scores[keep])
            keypoints.append(level_kps)
        if not boxes:
            return []

        all_boxes = np.concatenate(boxes) / scale
        all_scores = np.concatenate(scores)
        all_kps = np.concatenate(keypoints) / scale
        kept = nms(all_boxes, all_scores, self.nms_threshold)
        return [
            Detection(
                bbox=(
                    float(all_boxes[i][0]),
                    float(all_boxes[i][1]),
                    float(all_boxes[i][2]),
                    float(all_boxes[i][3]),
                ),
                score=float(all_scores[i]),
                kps=all_kps[i].astype(np.float32),
            )
            for i in kept
        ]
