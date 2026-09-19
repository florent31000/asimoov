"""IoU face tracker with a hysteresis before declaring a person lost.

A track that stops matching is kept alive for ``lost_after_s`` (1 s by
default) so a blink, a head turn, or one dropped frame does not produce a
``person_lost`` / ``person_seen`` flicker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from asimoov.perception.face_id.gallery import UNKNOWN

DEFAULT_IOU_THRESHOLD = 0.3
DEFAULT_LOST_AFTER_S = 1.0

Box = tuple[float, float, float, float]


def iou(a: Box, b: Box) -> float:
    """Intersection over union of two ``(x0, y0, x1, y1)`` boxes."""
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Track:
    """One tracked face across frames."""

    track_id: str
    bbox: Box
    score: float
    first_seen: float
    last_seen: float
    kps: Any = None
    person_id: str | None = None
    name: str | None = None
    identity_status: str = UNKNOWN
    candidate_person_id: str | None = None
    candidate_name: str | None = None
    match_score: float = 0.0
    quality: float = 0.0
    last_embedded_at: float = 0.0
    frames: int = 1


@dataclass
class TrackUpdate:
    """Result of one ``IoUTracker.update`` call."""

    visible: list[Track] = field(default_factory=list)
    lost: list[Track] = field(default_factory=list)


class IoUTracker:
    """Greedy IoU association, newest-detection-wins on ties."""

    def __init__(
        self,
        *,
        iou_threshold: float = DEFAULT_IOU_THRESHOLD,
        lost_after_s: float = DEFAULT_LOST_AFTER_S,
    ) -> None:
        self.iou_threshold = iou_threshold
        self.lost_after_s = lost_after_s
        self.tracks: dict[str, Track] = {}
        self._next_id = 1

    def update(self, detections: list[Any], now: float) -> TrackUpdate:
        """Associate ``detections`` (objects with ``bbox``/``score``/``kps``).

        Returns the tracks seen in this frame and those whose hysteresis
        expired, in that order.
        """
        pairs = [
            (iou(track.bbox, detection.bbox), track_id, index)
            for track_id, track in self.tracks.items()
            for index, detection in enumerate(detections)
        ]
        pairs.sort(key=lambda item: item[0], reverse=True)

        used_tracks: set[str] = set()
        used_detections: set[int] = set()
        update = TrackUpdate()
        for overlap, track_id, index in pairs:
            if overlap < self.iou_threshold:
                break
            if track_id in used_tracks or index in used_detections:
                continue
            used_tracks.add(track_id)
            used_detections.add(index)
            track = self.tracks[track_id]
            detection = detections[index]
            track.bbox = detection.bbox
            track.score = detection.score
            track.kps = getattr(detection, "kps", None)
            track.last_seen = now
            track.frames += 1
            update.visible.append(track)

        for index, detection in enumerate(detections):
            if index in used_detections:
                continue
            track = Track(
                track_id=self._new_id(),
                bbox=detection.bbox,
                score=detection.score,
                first_seen=now,
                last_seen=now,
                kps=getattr(detection, "kps", None),
            )
            self.tracks[track.track_id] = track
            update.visible.append(track)

        for track_id, track in list(self.tracks.items()):
            if now - track.last_seen >= self.lost_after_s:
                del self.tracks[track_id]
                update.lost.append(track)
        return update

    def drop_all(self) -> list[Track]:
        """Forget every track (camera closed); returns the tracks dropped."""
        lost = list(self.tracks.values())
        self.tracks.clear()
        return lost

    def _new_id(self) -> str:
        track_id = f"t{self._next_id}"
        self._next_id += 1
        return track_id
