"""Test compute_streak_update (WP-7 Ф48): гонка touch/record счётчика активных дней.

Pure-function unit test — не требует БД. Кейс 2 (post-reset) — ровно та регрессия,
которую нашёл Codex в peer-review (turn 3, 2026-08-04-27-wp7-f48-bot-reliability):
без active_days_streak > 0 в условии, пользователь после сброса статистики с ещё
не удалённым вчерашним daily_activity_marker получил бы продолжение чужой серии.
"""
from db.queries.activity import compute_streak_update


def test_continues_streak_when_active_yesterday():
    new_streak, new_longest = compute_streak_update(current_streak=3, had_yesterday=True, current_longest=5)
    assert new_streak == 4
    assert new_longest == 5


def test_breaks_streak_when_not_active_yesterday():
    new_streak, new_longest = compute_streak_update(current_streak=5, had_yesterday=False, current_longest=5)
    assert new_streak == 1
    assert new_longest == 5


def test_post_reset_does_not_inherit_stale_yesterday_marker():
    """Регрессия: streak=0 (после сброса) + вчерашний маркер ещё не удалён → НЕ продолжаем."""
    new_streak, new_longest = compute_streak_update(current_streak=0, had_yesterday=True, current_longest=10)
    assert new_streak == 1
    assert new_longest == 10


def test_new_record_updates_longest_streak():
    new_streak, new_longest = compute_streak_update(current_streak=5, had_yesterday=True, current_longest=5)
    assert new_streak == 6
    assert new_longest == 6


def test_first_ever_active_day_with_null_longest():
    new_streak, new_longest = compute_streak_update(current_streak=0, had_yesterday=False, current_longest=None)
    assert new_streak == 1
    assert new_longest == 1
