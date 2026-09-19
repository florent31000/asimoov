"""Session thresholds: renew on tokens or age, disconnect when idle, truncate."""

from __future__ import annotations

from asimoov.core.session import SessionState, SessionThresholds

NOW = 1_000_000.0


def state(**kwargs):
    return SessionState(started_at=NOW, last_activity_at=NOW, **kwargs)


def test_a_fresh_session_needs_nothing():
    session = state()
    assert session.renew_reason(NOW + 60) is None
    assert session.should_disconnect(NOW + 60) is False
    assert session.items_to_delete() == 0


def test_renew_on_tokens():
    session = state(total_tokens=24_001)
    assert session.renew_reason(NOW) == "tokens"


def test_renew_on_age():
    session = state()
    assert session.renew_reason(NOW + 25 * 60 + 1) == "age"
    assert session.renew_reason(NOW + 24 * 60) is None


def test_disconnect_when_idle():
    session = state()
    assert session.should_disconnect(NOW + 10 * 60 - 1) is False
    assert session.should_disconnect(NOW + 10 * 60 + 1) is True


def test_touch_resets_the_idle_timer():
    session = state()
    session.touch(NOW + 9 * 60, total_tokens=100, items=5)
    assert session.should_disconnect(NOW + 18 * 60) is False
    assert (session.total_tokens, session.items) == (100, 5)


def test_items_beyond_the_limit_are_truncated():
    assert state(items=45).items_to_delete() == 5


def test_thresholds_are_configurable():
    session = SessionState(
        started_at=NOW,
        last_activity_at=NOW,
        total_tokens=100,
        thresholds=SessionThresholds(max_total_tokens=50),
    )
    assert session.renew_reason(NOW) == "tokens"
