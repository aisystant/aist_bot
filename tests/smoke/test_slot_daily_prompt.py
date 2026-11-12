"""Slot daily prompt goes through the delivery layer (WP-568 F16 step 1b).

Observable contract: every recipient is offered to enqueue() as an optional
(capped) message with a per-user Moscow-day key and the five slot buttons; the
scheduler itself sends nothing, and one failed enqueue does not stop the rest.
"""
from datetime import datetime

import pytest

import core.notification_service as ns
import core.scheduler as sch


class _Conn:
    def __init__(self, user_ids):
        self._rows = [{"user_id": uid} for uid in user_ids]

    async def fetch(self, sql, *args):
        return self._rows


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False
        return _Acq()


@pytest.fixture
def _scheduler_env(monkeypatch):
    import db.connection as dbc

    def _setup(user_ids, enqueue):
        pool = _Pool(_Conn(user_ids))

        async def _get_pool():
            return pool
        monkeypatch.setattr(dbc, "get_pool", _get_pool)
        monkeypatch.setattr(ns, "enqueue", enqueue)
        monkeypatch.setattr(
            sch, "moscow_now", lambda: datetime(2026, 11, 12, 22, 0, tzinfo=sch.MOSCOW_TZ)
        )
    return _setup


@pytest.mark.asyncio
async def test_slot_prompt_enqueues_capped_with_day_key(_scheduler_env):
    calls = []

    async def _enqueue(chat_id, klass, content, **kw):
        calls.append((chat_id, klass, content, kw))
        return {"notification_id": 1, "status": "queued"}

    _scheduler_env([111, 222], _enqueue)
    await sch._send_slot_daily_prompt()

    assert [c[0] for c in calls] == [111, 222]
    chat_id, klass, content, kw = calls[0]
    assert klass == ns.CLASS_CAPPED
    assert kw["dedup_key"] == kw["journal_key"] == "slot_daily_prompt:111:2026-11-12"
    assert kw["journal_type"] == "slot_daily_prompt"
    assert [a["action"] for a in content["actions"]] == [
        "slot_daily:0.5", "slot_daily:1.0", "slot_daily:2.0",
        "slot_daily:custom", "slot_daily:skip",
    ]


@pytest.mark.asyncio
async def test_slot_prompt_continues_after_one_enqueue_failure(_scheduler_env):
    offered = []

    async def _enqueue(chat_id, klass, content, **kw):
        offered.append(chat_id)
        if chat_id == 111:
            raise RuntimeError("queue down")
        return {"notification_id": 2, "status": "queued"}

    _scheduler_env([111, 222], _enqueue)
    await sch._send_slot_daily_prompt()

    assert offered == [111, 222]
