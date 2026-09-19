"""Cosine matching, the 0.45 / 0.6 bands, and smoothing over 3 comparisons."""

from __future__ import annotations

import math

import numpy as np
import pytest

from asimoov.perception.face_id.gallery import (
    IDENTIFIED,
    UNCERTAIN,
    UNKNOWN,
    Gallery,
    Match,
    MatchSmoother,
    classify,
)


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def at_angle(cosine: float) -> np.ndarray:
    """A 2-d unit vector whose cosine with (1, 0) is exactly `cosine`."""
    return np.array([cosine, math.sqrt(max(0.0, 1 - cosine**2))], dtype=np.float32)


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0.0, UNKNOWN), (0.44, UNKNOWN), (0.45, UNCERTAIN), (0.59, UNCERTAIN), (0.6, IDENTIFIED)],
)
def test_bands(score: float, expected: str):
    assert classify(score) == expected


def test_empty_gallery_matches_nothing():
    assert Gallery().match(unit(1, 0)).person_id is None


def test_identical_embedding_scores_one():
    gallery = Gallery()
    gallery.add("p_sam", unit(1, 0), name="Sam")
    match = gallery.match(unit(1, 0))
    assert match.person_id == "p_sam"
    assert match.name == "Sam"
    assert match.score == pytest.approx(1.0, abs=1e-6)
    assert match.status == IDENTIFIED


def test_a_stranger_stays_unknown():
    gallery = Gallery()
    gallery.add("p_sam", unit(1, 0))
    match = gallery.match(at_angle(0.2))
    assert match.score == pytest.approx(0.2, abs=1e-6)
    assert match.status == UNKNOWN


def test_a_borderline_face_is_uncertain():
    gallery = Gallery()
    gallery.add("p_sam", unit(1, 0))
    assert gallery.match(at_angle(0.52)).status == UNCERTAIN


def test_the_best_of_several_embeddings_wins():
    gallery = Gallery()
    gallery.add("p_sam", at_angle(0.3))
    gallery.add("p_sam", unit(1, 0))
    gallery.add("p_caro", at_angle(-0.9))
    match = gallery.match(unit(1, 0))
    assert match.person_id == "p_sam"
    assert match.score == pytest.approx(1.0, abs=1e-6)


def test_unnormalized_input_is_normalized():
    gallery = Gallery()
    gallery.add("p_sam", np.array([3.0, 0.0], dtype=np.float32))
    assert gallery.match(np.array([7.0, 0.0], dtype=np.float32)).score == pytest.approx(1.0)


def test_zero_embedding_is_rejected():
    with pytest.raises(ValueError, match="zero embedding"):
        Gallery().add("p_sam", np.zeros(4, dtype=np.float32))


def sam(score: float) -> Match:
    return Match(person_id="p_sam", name="Sam", score=score, status=classify(score))


def test_one_lucky_frame_does_not_identify_on_its_own():
    smoother = MatchSmoother()
    smoother.update("t1", sam(0.30))
    smoother.update("t1", sam(0.30))
    smoothed = smoother.update("t1", sam(0.95))
    assert smoothed.score == pytest.approx((0.30 + 0.30 + 0.95) / 3)
    assert smoothed.status == UNCERTAIN


def test_three_good_frames_identify():
    smoother = MatchSmoother()
    for _ in range(3):
        smoothed = smoother.update("t1", sam(0.71))
    assert smoothed.status == IDENTIFIED
    assert smoothed.person_id == "p_sam"


def test_the_window_only_keeps_the_last_three():
    smoother = MatchSmoother()
    for score in (0.1, 0.1, 0.1, 0.8, 0.8, 0.8):
        smoothed = smoother.update("t1", sam(score))
    assert smoothed.score == pytest.approx(0.8)


def test_the_dominant_person_wins_over_a_single_outlier():
    smoother = MatchSmoother()
    smoother.update("t1", Match("p_caro", "Caro", 0.62, IDENTIFIED))
    smoother.update("t1", sam(0.65))
    smoothed = smoother.update("t1", sam(0.68))
    assert smoothed.person_id == "p_sam"
    assert smoothed.score == pytest.approx((0.65 + 0.68) / 2)


def test_tracks_are_smoothed_independently():
    smoother = MatchSmoother()
    smoother.update("t1", sam(0.9))
    smoothed = smoother.update("t2", sam(0.2))
    assert smoothed.score == pytest.approx(0.2)


def test_forgetting_a_track_resets_its_window():
    smoother = MatchSmoother()
    smoother.update("t1", sam(0.9))
    smoother.forget("t1")
    assert smoother.update("t1", sam(0.2)).score == pytest.approx(0.2)
