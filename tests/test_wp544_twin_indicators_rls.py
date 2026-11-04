"""/me fallback reads indicators tables under the RLS account context (WP-544 F16).

digital_twins and calculated_profile in the indicators database are under row-level
security keyed on app.account_id. Without the owner role the reads only return rows
when the context is set, so they run in one transaction on one connection after
set_config. No network or database here: the pools are doubles.
"""

import os
import sys
from datetime import datetime
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import db.connection as db_connection  # noqa: E402
from handlers import twin  # noqa: E402

ACCOUNT_ID = "00000000-0000-0000-0000-0000000000c1"
SET_CONFIG = "SELECT set_config('app.account_id', $1, true)"

ENGAGEMENT_ROW = {
    "sessions_total": 1,
    "ai_chats_total": 0,
    "marathon_steps_total": 0,
    "marathon_tasks_total": 0,
    "feed_completed_total": 0,
    "training_attempts_total": 0,
    "training_passed_total": 0,
    "assessments_total": 0,
    "events_total": 1,
    "first_event_at": datetime(2026, 1, 1),
    "last_event_at": None,
    "active_days": 1,
    "events_last_7d": 0,
    "events_last_30d": 1,
}


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.events.append("begin")

    async def __aexit__(self, *exc_info):
        self.conn.events.append("end")
        return False


class _Conn:
    def __init__(self, fetchrow=None, fetchval=None, fail_on=None):
        self._fetchrow = fetchrow
        self._fetchval = fetchval or {}
        self.fail_on = fail_on
        self.events = []

    def transaction(self):
        return _Tx(self)

    async def execute(self, sql, *args):
        self.events.append((sql, args))

    async def fetchrow(self, sql, *args):
        return self._fetchrow

    async def fetchval(self, sql, *args):
        self.events.append((sql, args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("new row violates policy for user 'secret-value'")
        for marker, value in self._fetchval.items():
            if marker in sql:
                return value
        return None


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc_info):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.acquired = 0

    def acquire(self):
        self.acquired += 1
        return _Acquire(self.conn)


def _patch_pools(monkeypatch, indicators_conn):
    pools = {
        "get_pool": _Pool(_Conn(fetchrow=ENGAGEMENT_ROW)),
        "get_persona_pool": _Pool(_Conn(fetchval={"ory_identity": ACCOUNT_ID})),
        "get_indicators_pool": _Pool(indicators_conn),
    }
    for name, pool in pools.items():

        async def getter(pool=pool):
            return pool

        monkeypatch.setattr(db_connection, name, getter)
    return pools["get_indicators_pool"]


@pytest.mark.asyncio
async def test_reads_run_after_set_config_in_one_transaction(monkeypatch):
    indicators_conn = _Conn(
        fetchval={
            "2_collected": {"2_7_iwe": {"sessions": 3}},
            "calculated_profile": {"3_derived": {"x": 1}},
        }
    )
    indicators_pool = _patch_pools(monkeypatch, indicators_conn)

    collected = await twin._fallback_engagement(42)

    assert collected["2_7_iwe"] == {"sessions": 3}
    assert collected["_derived"] == {"x": 1}
    assert indicators_pool.acquired == 1
    events = indicators_conn.events
    assert events[0] == "begin"
    assert events[1] == (SET_CONFIG, (ACCOUNT_ID,))
    assert events[-1] == "end"
    reads = events[2:-1]
    assert [sql.split("FROM ")[1].split()[0] for sql, _ in reads] == [
        "public.digital_twins",
        "public.calculated_profile",
    ]
    assert all(args == (ACCOUNT_ID,) for _, args in reads)


@pytest.mark.asyncio
async def test_failed_read_logs_table_without_values(monkeypatch):
    indicators_conn = _Conn(fail_on="calculated_profile")
    _patch_pools(monkeypatch, indicators_conn)

    with patch.object(twin, "logger") as logger:
        collected = await twin._fallback_engagement(42)

    assert "_derived" not in collected
    logger.warning.assert_called_once()
    args = logger.warning.call_args.args
    assert args[1] == "calculated_profile"
    assert args[2] == "RuntimeError"
    logged = " ".join(str(a) for a in args)
    assert "secret-value" not in logged
    assert ACCOUNT_ID not in logged
