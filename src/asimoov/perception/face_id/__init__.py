"""Face detection, tracking, identification, and enrolment."""

from __future__ import annotations

from asimoov.perception.face_id.enroll import Enrollment, EnrollmentResult
from asimoov.perception.face_id.gallery import Gallery, Match, MatchSmoother, classify
from asimoov.perception.face_id.module import ENROLL_COMMAND, FaceIdModule
from asimoov.perception.face_id.tracker import IoUTracker, Track, TrackUpdate, iou

__all__ = [
    "ENROLL_COMMAND",
    "Enrollment",
    "EnrollmentResult",
    "FaceIdModule",
    "Gallery",
    "IoUTracker",
    "Match",
    "MatchSmoother",
    "Track",
    "TrackUpdate",
    "classify",
    "iou",
]
