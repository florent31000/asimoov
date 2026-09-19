"""The `face_id` perception module: camera -> SCRFD -> tracker -> gallery -> percepts.

Publishes `percept.person_seen` at camera rate for every visible track and
`percept.person_lost` once the tracker's hysteresis expires. Frames are
decoded, measured, and dropped: this module never stores an image and never
publishes one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from asimoov.contracts.memory import Person
from asimoov.contracts.perception import PerceptionContext, PerceptionModule
from asimoov.contracts.percepts import Bearing, PersonLost, PersonSeen
from asimoov.perception.camera.base import CameraSource
from asimoov.perception.face_id.embedder_arcface import align_face, face_quality
from asimoov.perception.face_id.enroll import (
    DEFAULT_MIN_QUALITY,
    DEFAULT_SAMPLES,
    DEFAULT_TIMEOUT_S,
    Enrollment,
    EnrollmentResult,
)
from asimoov.perception.face_id.gallery import (
    IDENTIFIED,
    UNCERTAIN,
    UNKNOWN,
    Match,
    MatchSmoother,
    classify,
)
from asimoov.perception.face_id.tracker import IoUTracker, Track
from asimoov.perception.geometry import bearing_from_bbox, distance_class_from_bbox
from asimoov.perception.models import EMBEDDING_MODEL_ID

log = logging.getLogger(__name__)

PERSON_SEEN_TOPIC = "percept.person_seen"
PERSON_LOST_TOPIC = "percept.person_lost"
ENROLL_COMMAND = "face_id.enroll"
RELOAD_GALLERY_COMMAND = "face_id.reload_gallery"

DEFAULT_FOV_H_DEG = 70.0
EMBED_INTERVAL_S = 1.0
FPS_LOG_INTERVAL_S = 30.0


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


class FaceIdModule(PerceptionModule):
    """Face detection, tracking, identification, and enrolment.

    The detector, embedder, and store are injected so the pipeline can be
    exercised with stubs and without any model download.
    """

    def __init__(
        self,
        camera: CameraSource,
        *,
        store: Any,
        detector: Any,
        embedder: Any,
        fov_h_deg: float = DEFAULT_FOV_H_DEG,
        enroll_samples: int = DEFAULT_SAMPLES,
        enroll_min_quality: float = DEFAULT_MIN_QUALITY,
        enroll_timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.camera = camera
        self.store = store
        self.detector = detector
        self.embedder = embedder
        self.fov_h_deg = fov_h_deg
        self.tracker = IoUTracker()
        self.smoother = MatchSmoother()
        self.enrollment: Enrollment | None = None
        self._enroll_samples = enroll_samples
        self._enroll_min_quality = enroll_min_quality
        self._enroll_timeout_s = enroll_timeout_s
        self._ctx: PerceptionContext | None = None
        self._task: asyncio.Task[None] | None = None
        self._names: dict[str, str | None] = {}
        self._frames = 0
        self._fps = 0.0
        self._fps_since = 0.0

    @property
    def fps(self) -> float:
        """Measured end-to-end pipeline rate over the last window."""
        return self._fps

    # -- PerceptionModule --------------------------------------------------

    async def start(self, ctx: PerceptionContext) -> None:
        self._ctx = ctx
        self._fps_since = time.monotonic()
        self._task = asyncio.create_task(self._run(), name="face_id")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.enrollment is not None:
            self.enrollment.cancel()
            self.enrollment = None
        await self.camera.stop()

    async def handle_command(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name == RELOAD_GALLERY_COMMAND:
            return await self._reload_gallery()
        if name != ENROLL_COMMAND:
            raise KeyError(name)
        track_id = params.get("track_id")
        person_id = params.get("person_id")
        if not track_id or not person_id:
            return {"ok": False, "reason": "track_id and person_id are required"}
        if track_id not in self.tracker.tracks:
            return {"ok": False, "reason": "unknown track_id", "track_id": track_id}
        if self.enrollment is not None and not self.enrollment.done:
            self.enrollment.cancel()
        self.enrollment = Enrollment(
            track_id=track_id,
            person_id=person_id,
            name=params.get("name"),
            started_at=time.monotonic(),
            samples=self._enroll_samples,
            min_quality=self._enroll_min_quality,
            timeout_s=self._enroll_timeout_s,
        )
        result = await self._await_enrollment(self.enrollment)
        if result.ok:
            await self._persist_enrollment(self.enrollment, result)
        self.enrollment = None
        return result.to_payload()

    # -- pipeline ----------------------------------------------------------

    async def _run(self) -> None:
        await self.camera.start()
        try:
            while True:
                frame = await self.camera.read()
                if frame is None:
                    break
                await self._process(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("face_id pipeline stopped")
            raise
        finally:
            await self._flush_tracks()

    async def _process(self, frame: Any) -> None:
        # Tracking and the embedding cadence run on capture time, not on
        # processing time: a slow frame must not age a track.
        now = frame.ts
        detections = await asyncio.to_thread(self.detector.detect, frame.bgr)
        update = self.tracker.update(detections, now)

        for track in update.visible:
            if self._needs_embedding(track, now):
                await self._identify(track, frame, now)
            await self._publish_seen(track, frame)

        for track in update.lost:
            self.smoother.forget(track.track_id)
            if self.enrollment is not None and self.enrollment.track_id == track.track_id:
                self.enrollment.track_lost()
            await self._publish_lost(track, frame)

        self._count_frame(time.monotonic())

    def _needs_embedding(self, track: Track, now: float) -> bool:
        enrolling = self.enrollment is not None and not self.enrollment.done
        if enrolling and track.track_id == self.enrollment.track_id:
            return True
        if track.last_embedded_at == 0.0:
            return True  # a new track is identified as soon as it appears
        return now - track.last_embedded_at >= EMBED_INTERVAL_S

    async def _identify(self, track: Track, frame: Any, now: float) -> None:
        if track.kps is None:
            return
        vector, quality = await asyncio.to_thread(self._embed_sync, frame.bgr, track)
        track.last_embedded_at = now
        track.quality = quality
        if self.enrollment is not None and not self.enrollment.done:
            self.enrollment.offer(track.track_id, vector, quality, now)
        raw = await self.store.match_face(vector)
        match = (
            Match(person_id=raw[0], name=None, score=raw[1], status=classify(raw[1]))
            if raw is not None
            else Match(person_id=None, name=None, score=0.0, status=UNKNOWN)
        )
        smoothed = self.smoother.update(track.track_id, match)
        track.match_score = smoothed.score
        track.identity_status = smoothed.status
        track.person_id = track.name = None
        track.candidate_person_id = track.candidate_name = None
        if smoothed.person_id is None:
            return
        if smoothed.status == IDENTIFIED:
            track.person_id = smoothed.person_id
            track.name = await self._name_of(smoothed.person_id)
        elif smoothed.status == UNCERTAIN:
            track.candidate_person_id = smoothed.person_id
            track.candidate_name = await self._name_of(smoothed.person_id)

    def _embed_sync(self, image: Any, track: Track) -> tuple[Any, float]:
        aligned = align_face(image, track.kps)
        quality = face_quality(
            aligned, det_score=track.score, bbox_height_px=track.bbox[3] - track.bbox[1]
        )
        return self.embedder.embed_aligned(aligned), quality

    async def _name_of(self, person_id: str) -> str | None:
        if person_id not in self._names:
            person = await self.store.get_person(person_id)
            self._names[person_id] = person.name if person is not None else None
        return self._names[person_id]

    # -- percepts ----------------------------------------------------------

    def _bbox_norm(self, track: Track, frame: Any) -> tuple[float, float, float, float]:
        width, height = float(frame.width), float(frame.height)
        x0, y0, x1, y1 = track.bbox
        return (
            _clip01(x0 / width),
            _clip01(y0 / height),
            _clip01(x1 / width),
            _clip01(y1 / height),
        )

    def _bearing(self, bbox_norm: tuple[float, float, float, float], frame: Any) -> Bearing:
        return bearing_from_bbox(
            bbox_norm, fov_h_deg=self.fov_h_deg, aspect=frame.height / frame.width
        )

    async def _publish_seen(self, track: Track, frame: Any) -> None:
        bbox_norm = self._bbox_norm(track, frame)
        percept = PersonSeen(
            track_id=track.track_id,
            confidence=track.match_score if track.person_id else track.score,
            bearing=self._bearing(bbox_norm, frame),
            distance_class=distance_class_from_bbox(bbox_norm),
            bbox_norm=bbox_norm,
            face_quality=track.quality,
            person_id=track.person_id,
            name=track.name,
            identity_status=track.identity_status,
            candidate_person_id=track.candidate_person_id,
            candidate_name=track.candidate_name,
            # v1.3: the similarity behind the candidate, so the mind can say
            # "this looks like Sam (0.52)" instead of asserting it is Sam.
            candidate_score=(
                track.match_score if track.candidate_person_id is not None else None
            ),
        )
        await self._publish(PERSON_SEEN_TOPIC, percept.to_dict())

    async def _publish_lost(self, track: Track, frame: Any) -> None:
        percept = PersonLost(
            track_id=track.track_id,
            last_bearing=self._bearing(self._bbox_norm(track, frame), frame),
            person_id=track.person_id,
        )
        await self._publish(PERSON_LOST_TOPIC, percept.to_dict())

    async def _publish(self, topic: str, data: dict[str, Any]) -> None:
        ctx = self._ctx
        if ctx is None or ctx.publish is None:
            return
        await ctx.publish(topic, data, kind="percept")

    async def _flush_tracks(self) -> None:
        for track in self.tracker.drop_all():
            self.smoother.forget(track.track_id)

    # -- enrolment ---------------------------------------------------------

    async def _await_enrollment(self, enrollment: Enrollment) -> EnrollmentResult:
        while True:
            result = enrollment.tick(time.monotonic())
            if result is not None:
                return result
            await asyncio.sleep(0.05)

    async def _persist_enrollment(
        self, enrollment: Enrollment, result: EnrollmentResult
    ) -> None:
        existing = await self.store.get_person(enrollment.person_id)
        await self.store.upsert_person(
            Person(
                id=enrollment.person_id,
                name=enrollment.name or (existing.name if existing else None),
                created_at=existing.created_at if existing else time.time(),
                last_seen_at=time.time(),
                relationship=existing.relationship if existing else None,
                notes=existing.notes if existing else None,
            )
        )
        for vector, quality in enrollment.collected:
            await self.store.add_face_embedding(
                enrollment.person_id, EMBEDDING_MODEL_ID, vector, quality
            )
        self._names.pop(enrollment.person_id, None)
        log.info("enrolled %s with %d samples", enrollment.person_id, result.samples)

    # -- gallery -----------------------------------------------------------

    async def _reload_gallery(self) -> dict[str, Any]:
        identities = await self.store.reload_gallery()
        self._names.clear()
        for track in self.tracker.tracks.values():
            self.smoother.forget(track.track_id)
            track.identity_status = UNKNOWN
            track.person_id = track.name = None
            track.candidate_person_id = track.candidate_name = None
            track.last_embedded_at = 0.0
        return {"ok": True, "identities": identities}

    # -- metrics -----------------------------------------------------------

    def _count_frame(self, now: float) -> None:
        self._frames += 1
        elapsed = now - self._fps_since
        if elapsed >= FPS_LOG_INTERVAL_S:
            self._fps = self._frames / elapsed
            log.info("face_id %.1f fps over %.0fs", self._fps, elapsed)
            self._frames = 0
            self._fps_since = now
