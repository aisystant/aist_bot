"""WP-568 Ф16 шаг 1б, вторая волна — напоминания через очередь доставки.

check_reminders hands each due reminder to the delivery queue and only then
marks it handled. Without a database: a fake learning connection that records
the order of the queue hand-over and the reminder updates.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import core.scheduler as scheduler  # noqa: E402


class FakeLearningConn:
    """public.reminder rows; fetchrow honours the per-call exclusion list ($4)."""

    def __init__(self, rows, log):
        self.rows = {r["id"]: dict(r, sent=False) for r in rows}
        self.log = log

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *a):
                return False
        return _Tx()

    async def fetchrow(self, sql, *args):
        excluded = set(args[3])
        for row in self.rows.values():
            if not row["sent"] and row["id"] not in excluded:
                return row
        return None

    async def execute(self, sql, *args):
        row = self.rows[args[0]]
        if "sent = TRUE" in sql:
            row["sent"] = True
        if "fail_count" in sql:
            row["fail_count"] = args[1]
        self.log.append(("update", args[0], row["sent"]))


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False
        return _Acq()


def _reminder(rid, chat=555, rtype="custom", text="позвонить", fail_count=0):
    return {"id": rid, "chat_id": chat, "reminder_type": rtype, "text": text,
            "fail_count": fail_count}


async def _run(rows, send_user, blocked_now=False, send_legacy=None):
    log = []
    conn = FakeLearningConn(rows, log)
    bot_data = AsyncMock()
    bot_data.fetchval = AsyncMock(return_value=blocked_now)

    async def _send_user(chat_id, text, rid):
        log.append(("queue", rid))
        return await send_user(chat_id, text, rid)

    with patch("db.connection.get_learning_pool", new=AsyncMock(return_value=_Pool(conn))), \
         patch.object(scheduler, "DELIVERY_LAYER_ENABLED", True), \
         patch.object(scheduler, "get_pool", new=AsyncMock(return_value=bot_data)), \
         patch.object(scheduler, "_get_blocked_chat_ids", new=AsyncMock(return_value=set())), \
         patch.object(scheduler, "send_user_reminder", new=_send_user), \
         patch.object(scheduler, "send_reminder", new=send_legacy or AsyncMock(return_value=None)):
        await scheduler.check_reminders()
    return conn, log


@pytest.mark.asyncio
async def test_reminder_is_queued_before_it_is_marked_sent():
    conn, log = await _run(
        [_reminder(1)],
        AsyncMock(return_value={"status": "queued", "notification_id": 7}),
    )
    assert log == [("queue", 1), ("update", 1, True)]
    assert conn.rows[1]["sent"] is True


@pytest.mark.asyncio
async def test_suppressed_reminder_is_handled_and_not_retried():
    # Daily cap / opt-out is a final outcome: marked handled, not re-queued each minute.
    conn, log = await _run(
        [_reminder(1)],
        AsyncMock(return_value={"status": "suppressed", "reason": "cap-exceeded"}),
    )
    assert conn.rows[1]["sent"] is True
    assert log.count(("queue", 1)) == 1


@pytest.mark.asyncio
async def test_user_blocked_meanwhile_is_skipped_once_and_kept_unsent():
    # Before the fix the reverted row was picked again at once, forever.
    send = AsyncMock()
    conn, log = await _run([_reminder(1), _reminder(2, chat=777)], send, blocked_now=True)
    assert send.await_count == 0
    assert conn.rows[1]["sent"] is False and conn.rows[2]["sent"] is False
    assert log == []


@pytest.mark.asyncio
async def test_queue_failure_counts_attempt_and_keeps_reminder():
    send = AsyncMock(side_effect=ConnectionError("bot_data down"))
    conn, log = await _run([_reminder(1, fail_count=0)], send)
    assert conn.rows[1]["sent"] is False
    assert conn.rows[1]["fail_count"] == 1
    assert log.count(("queue", 1)) == 1  # not retried within the same call


@pytest.mark.asyncio
async def test_third_queue_failure_gives_up():
    send = AsyncMock(side_effect=ConnectionError("bot_data down"))
    conn, _ = await _run([_reminder(1, fail_count=2)], send)
    assert conn.rows[1]["sent"] is True
    assert conn.rows[1]["fail_count"] == 3


@pytest.mark.asyncio
async def test_user_reminder_goes_to_queue_with_old_key_and_meta():
    enqueue = AsyncMock(return_value={"status": "queued", "notification_id": 3})
    with patch("core.notification_service.was_queue_row_sent", new=AsyncMock(return_value=False)), \
         patch("core.notification_service.enqueue", new=enqueue):
        res = await scheduler.send_user_reminder(555, "позвонить", 9)

    assert res["status"] == "queued"
    args, kwargs = enqueue.call_args
    assert args[0] == 555 and args[1] == "transactional"
    assert args[2]["text"] == "🔔 позвонить"
    assert args[2]["meta"] == {"reminder_type": "custom", "reminder_id": 9}
    assert kwargs == {"dedup_key": "user_remind:555:9", "journal_key": "user_remind:555:9",
                      "journal_type": "reminder"}


@pytest.mark.asyncio
async def test_nothing_due_reminder_is_marked_handled():
    # Legacy reminder for a user on the new marathon engine: nothing to send, done.
    conn, log = await _run([_reminder(1, rtype="+1h", text=None)], AsyncMock(),
                           send_legacy=AsyncMock(return_value=None))
    assert conn.rows[1]["sent"] is True
    assert log == [("update", 1, True)]


@pytest.mark.asyncio
async def test_reminders_are_held_while_delivery_layer_is_off():
    # Without the drain a queued reminder would never leave: keep it unsent.
    log = []
    conn = FakeLearningConn([_reminder(1)], log)
    with patch.object(scheduler, "DELIVERY_LAYER_ENABLED", False), \
         patch("db.connection.get_learning_pool", new=AsyncMock(return_value=_Pool(conn))) as pool:
        await scheduler.check_reminders()
    pool.assert_not_called()
    assert conn.rows[1]["sent"] is False


@pytest.mark.asyncio
async def test_legacy_reminder_goes_to_queue_as_capped_with_lesson_button():
    enqueue = AsyncMock(return_value={"status": "queued", "notification_id": 4})
    with patch("core.scheduler.get_intern", new=AsyncMock(return_value={"language": "ru"})), \
         patch("core.scheduler.get_topics_today", return_value=0), \
         patch("core.topics.get_marathon_day", return_value=3), \
         patch("core.topics.get_display_day", return_value=3), \
         patch("core.scheduler.moscow_now") as now, \
         patch("db.queries.marathon_newcomer.is_on_newcomer_marathon",
               new=AsyncMock(return_value=False)), \
         patch("core.scheduler._enqueue_reminder", new=enqueue):
        now.return_value.strftime.return_value = "2026-11-13"
        res = await scheduler.send_reminder(555, "+1h")

    assert res["status"] == "queued"
    chat_id, klass, key, spec = enqueue.call_args.args
    assert (chat_id, klass, key) == (555, "capped", "reminder:555:2026-11-13:+1h")
    assert spec["actions"][0]["action"] == "marathon_get_lesson"
    assert spec["meta"] == {"reminder_type": "+1h", "marathon_day": 3}
    assert spec["format"] == "markdown" and spec["text"]


@pytest.mark.asyncio
async def test_reminder_delivered_once_is_never_queued_again():
    # The journal write after a delivery may fail; the queue row still blocks a resend.
    enqueue = AsyncMock()
    with patch("core.notification_service.was_queue_row_sent", new=AsyncMock(return_value=True)), \
         patch("core.notification_service.enqueue", new=enqueue):
        res = await scheduler.send_user_reminder(555, "позвонить", 9)

    assert res == {"notification_id": None, "status": "suppressed", "reason": "already-sent"}
    enqueue.assert_not_called()
