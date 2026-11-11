"""Shown streak drops to 0 after a missed day (stored counter is stale until next activity)."""

from datetime import date

from db.queries.activity import effective_streak

TODAY = date(2026, 11, 11)


def test_streak_kept_when_active_today():
    assert effective_streak(5, TODAY, TODAY) == 5


def test_streak_kept_when_active_yesterday():
    assert effective_streak(5, date(2026, 11, 10), TODAY) == 5


def test_streak_zero_after_missed_day():
    assert effective_streak(2, date(2026, 11, 9), TODAY) == 0


def test_streak_zero_without_any_activity():
    assert effective_streak(3, None, TODAY) == 0


def test_stored_none_is_zero():
    assert effective_streak(None, TODAY, TODAY) == 0
