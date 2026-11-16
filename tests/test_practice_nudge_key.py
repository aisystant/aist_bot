"""WP-568: the practice hint key carries the lesson date.

The journal keeps keys forever. Without the date a participant who passes the same
marathon day again (restart, repeat) hit the old key and never got the hint.
"""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

import core.scheduler as sched


async def _run_with_users(users):
    enqueue = AsyncMock(return_value={"status": "queued"})
    with patch.object(sched, "_bot_token", "test-token"), \
         patch("db.queries.marathon_newcomer.get_users_for_practice_nudge",
               AsyncMock(return_value=users)), \
         patch("core.notification_service.enqueue", enqueue):
        await sched._send_practice_nudges()
    return enqueue


@pytest.mark.asyncio
async def test_key_contains_lesson_date_and_slot():
    sent_at = sched.moscow_now() - timedelta(minutes=40)
    users = [{"user_id": 555, "day_number": 7, "sent_at": sent_at,
              "lesson_date": "2026-11-16"}]
    enqueue = await _run_with_users(users)
    assert enqueue.await_count == 1
    kwargs = enqueue.await_args.kwargs
    assert kwargs["journal_key"] == "marathon_practice_nudge:555:7:2026-11-16:30m"
    assert kwargs["dedup_key"] == kwargs["journal_key"]


@pytest.mark.asyncio
async def test_repeated_marathon_day_gets_a_different_key():
    sent_at = sched.moscow_now() - timedelta(minutes=40)
    first = await _run_with_users([{"user_id": 555, "day_number": 7, "sent_at": sent_at,
                                    "lesson_date": "2026-11-10"}])
    second = await _run_with_users([{"user_id": 555, "day_number": 7, "sent_at": sent_at,
                                     "lesson_date": "2026-11-16"}])
    key_first = first.await_args.kwargs["journal_key"]
    key_second = second.await_args.kwargs["journal_key"]
    assert key_first != key_second


@pytest.mark.asyncio
async def test_late_slot_uses_150m_suffix():
    sent_at = sched.moscow_now() - timedelta(minutes=200)
    enqueue = await _run_with_users([{"user_id": 555, "day_number": 7, "sent_at": sent_at,
                                      "lesson_date": "2026-11-16"}])
    assert enqueue.await_args.kwargs["journal_key"].endswith(":2026-11-16:150m")
