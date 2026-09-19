"""In-RAM face gallery: cosine matching and the 0.45 / 0.6 decision bands.

Cosine similarity below 0.45 is ``unknown``, 0.45-0.6 is ``uncertain`` (the
mind may ask "are you Sam?"), above 0.6 is ``identified``. A single frame
never decides on its own: ``MatchSmoother`` averages the last 3 comparisons of
a track.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

import numpy as np

UNCERTAIN_THRESHOLD = 0.45
IDENTIFIED_THRESHOLD = 0.6
SMOOTHING_WINDOW = 3

UNKNOWN = "unknown"
UNCERTAIN = "uncertain"
IDENTIFIED = "identified"


def classify(score: float) -> str:
    """Map a cosine score to ``unknown`` / ``uncertain`` / ``identified``."""
    if score >= IDENTIFIED_THRESHOLD:
        return IDENTIFIED
    if score >= UNCERTAIN_THRESHOLD:
        return UNCERTAIN
    return UNKNOWN


@dataclass(frozen=True)
class Match:
    """The gallery's opinion about one embedding."""

    person_id: str | None
    name: str | None
    score: float
    status: str


NO_MATCH = Match(person_id=None, name=None, score=0.0, status=UNKNOWN)


@dataclass(frozen=True)
class GalleryEntry:
    """One stored embedding, already L2-normalized."""

    person_id: str
    vec: np.ndarray
    name: str | None = None


class Gallery:
    """Every enrolled embedding, matched by cosine similarity.

    A person with several embeddings scores as their best one, which is what
    makes enrolling 5 samples under different angles worthwhile.
    """

    def __init__(self, entries: list[GalleryEntry] | None = None) -> None:
        self._entries: list[GalleryEntry] = list(entries or [])
        self._matrix: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def person_ids(self) -> list[str]:
        return sorted({entry.person_id for entry in self._entries})

    def add(self, person_id: str, vec: np.ndarray, name: str | None = None) -> None:
        """Add one embedding (normalized on the way in)."""
        vector = np.asarray(vec, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise ValueError("cannot add a zero embedding to the gallery")
        self._entries.append(GalleryEntry(person_id=person_id, vec=vector / norm, name=name))
        self._matrix = None

    def clear(self) -> None:
        self._entries.clear()
        self._matrix = None

    def match(self, vec: np.ndarray) -> Match:
        """Best match for ``vec``; ``NO_MATCH`` when the gallery is empty."""
        if not self._entries:
            return NO_MATCH
        if self._matrix is None:
            self._matrix = np.stack([entry.vec for entry in self._entries])
        query = np.asarray(vec, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(query))
        if norm == 0.0:
            raise ValueError("cannot match a zero embedding")
        scores = self._matrix @ (query / norm)
        best = int(np.argmax(scores))
        entry = self._entries[best]
        score = float(scores[best])
        return Match(
            person_id=entry.person_id, name=entry.name, score=score, status=classify(score)
        )


class MatchSmoother:
    """Per-track rolling average over the last ``window`` comparisons.

    The winning person is the one with the highest summed score in the
    window; the reported score is that person's average, so one lucky frame
    cannot promote a stranger to ``identified``.
    """

    def __init__(self, window: int = SMOOTHING_WINDOW) -> None:
        self.window = window
        self._history: dict[str, deque[Match]] = {}

    def update(self, track_id: str, match: Match) -> Match:
        history = self._history.setdefault(track_id, deque(maxlen=self.window))
        history.append(match)
        totals: dict[str, float] = defaultdict(float)
        counts: dict[str, int] = defaultdict(int)
        names: dict[str, str | None] = {}
        for item in history:
            if item.person_id is None:
                continue
            totals[item.person_id] += item.score
            counts[item.person_id] += 1
            names[item.person_id] = item.name
        if not totals:
            return NO_MATCH
        person_id = max(totals, key=lambda key: totals[key])
        score = totals[person_id] / counts[person_id]
        return Match(
            person_id=person_id, name=names[person_id], score=score, status=classify(score)
        )

    def forget(self, track_id: str) -> None:
        self._history.pop(track_id, None)
