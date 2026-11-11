"""Bot events reach the central journal under the platform account id (WP-5 Ф74)."""

import asyncio
from types import SimpleNamespace

import pytest

from db.queries import events

ORY_ID = "25d91dbb-0000-4000-8000-000000000001"


@pytest.fixture
def wiring(monkeypatch):
    """Replace I/O around _record_event; collect gateway writes."""
    state = SimpleNamespace(posted=[], legacy=(101, False), consent=None, ory=ORY_ID)

    async def resolve(chat_id):
        return state.ory

    async def insert_legacy(*args, **kwargs):
        return state.legacy

    async def post_event(**kwargs):
        state.posted.append(kwargs)

    async def get_consent(account_id):
        state.consent_lookup = account_id
        return state.consent

    monkeypatch.setattr("helpers.dual_write.resolve_ory_id_from_chat", resolve)
    monkeypatch.setattr("db.queries.consent.get_consent", get_consent)
    monkeypatch.setattr(events, "_insert_legacy_event", insert_legacy)
    monkeypatch.setattr(events, "post_event", post_event)
    return state


async def _flush():
    await asyncio.sleep(0)


async def test_gateway_gets_platform_account_id(wiring):
    event_id = await events.log_event(42, "progress_viewed", {"view_type": "short"})
    await _flush()

    assert event_id == 101
    assert len(wiring.posted) == 1
    assert wiring.posted[0]["account_id"] == ORY_ID
    assert wiring.posted[0]["payload"]["activity_domain"] == "work"
    assert wiring.consent_lookup == ORY_ID


async def test_gateway_written_when_legacy_insert_fails(wiring):
    wiring.legacy = (None, False)

    event_id = await events.log_event(42, "session_start")
    await _flush()

    assert event_id is None
    assert [p["event_type"] for p in wiring.posted] == ["session_start"]


async def test_opt_out_by_platform_account_writes_nothing(wiring):
    wiring.consent = {"opt_in": False}

    assert await events.log_event(42, "session_start") is None
    await _flush()

    assert wiring.posted == []


async def test_opt_in_row_is_read_as_dict_and_writes_event(wiring):
    # get_consent returns a ConsentRow TypedDict, i.e. a plain dict at runtime.
    wiring.consent = {"account_id": ORY_ID, "opt_in": True, "scope": [], "opted_at": None}

    assert await events.log_event(42, "session_start") == 101
    await _flush()

    assert [p["event_type"] for p in wiring.posted] == ["session_start"]


async def test_dedup_skip_does_not_duplicate_in_journal(wiring):
    wiring.legacy = (None, True)

    assert await events.log_event(42, "session_start") is None
    await _flush()

    assert wiring.posted == []


async def test_backfill_keeps_occurred_at(wiring):
    from datetime import datetime

    past = datetime(2026, 11, 1, 9, 30)
    await events.log_event_with_occurred_at(42, "slot_logged", {"source": "self_report_backfill"}, occurred_at=past)
    await _flush()

    assert wiring.posted[0]["occurred_at"] == past
    assert wiring.posted[0]["account_id"] == ORY_ID


async def test_unlinked_user_goes_to_journal_without_account(wiring):
    wiring.ory = None

    await events.log_event(42, "session_start")
    await _flush()

    assert wiring.posted[0]["account_id"] is None


class _FakeConn:
    def __init__(self, fail_first):
        self.fail_first = fail_first
        self.sql = []

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if self.fail_first and len(self.sql) == 1:
            import asyncpg
            raise asyncpg.exceptions.UndefinedColumnError('column "external_id" does not exist')
        return {"id": 7}


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.parametrize("fail_first, expected_inserts", [(False, 1), (True, 2)])
async def test_legacy_insert_falls_back_without_external_id(monkeypatch, fail_first, expected_inserts):
    conn = _FakeConn(fail_first)

    async def get_pool():
        return _FakePool(conn)

    async def get_user_uuid(chat_id):
        return "legacy-uuid"

    monkeypatch.setattr(events, "get_pool", get_pool)
    monkeypatch.setattr("db.queries.identity.get_user_uuid", get_user_uuid)

    result = await events._insert_legacy_event(42, "session_start", None, 1.0, None, "bot", "ext-1", None)

    assert result == (7, False)
    assert len(conn.sql) == expected_inserts
    assert "external_id" not in conn.sql[-1] or not fail_first
    assert "COALESCE" in conn.sql[-1]  # database clock when no occurred_at
