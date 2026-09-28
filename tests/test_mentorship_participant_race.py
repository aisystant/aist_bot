"""
WP-578 - get_or_create_participant under a concurrent first message: with
UNIQUE(account_id, stream_id) in the database the losing INSERT raises
UniqueViolationError, which must not abort the outer RLS transaction and must
resolve to the winner's row instead of failing the archive write.
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import asyncpg
import pytest
from unittest.mock import AsyncMock


class _FakeConn:
    """Scripted connection: fetchrow pops the next outcome (a row dict, None or an exception)."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.executed = []
        self.fetched = []
        self.transactions_opened = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions_opened += 1
        yield

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetchrow(self, sql, *args):
        self.fetched.append((sql, args))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


def _use_pool(monkeypatch, conn):
    import db.queries.mentorship as queries

    monkeypatch.setattr(queries, "get_mentorship_pool", AsyncMock(return_value=_FakePool(conn)))
    return queries


def _is_insert(sql):
    return sql.lstrip().upper().startswith("INSERT")


@pytest.mark.asyncio
async def test_existing_row_is_returned_without_insert(monkeypatch):
    conn = _FakeConn([{"id": 7}])
    queries = _use_pool(monkeypatch, conn)

    participant_id = await queries.get_or_create_participant("reader-1", "S1", "participant-1")

    assert participant_id == 7
    assert not any(_is_insert(sql) for sql, _ in conn.fetched)
    assert conn.fetched[0][1] == ("participant-1", "S1")


@pytest.mark.asyncio
async def test_missing_row_is_inserted_inside_a_savepoint(monkeypatch):
    conn = _FakeConn([None, {"id": 99}])
    queries = _use_pool(monkeypatch, conn)

    participant_id = await queries.get_or_create_participant("reader-1", "S1", "participant-1")

    assert participant_id == 99
    assert [_is_insert(sql) for sql, _ in conn.fetched] == [False, True]
    # outer RLS transaction + the savepoint around the INSERT
    assert conn.transactions_opened == 2
    assert conn.executed[0][1] == ("reader-1",)  # RLS context is the reader's account


@pytest.mark.asyncio
async def test_lost_race_rereads_the_winners_row(monkeypatch):
    conn = _FakeConn([None, asyncpg.UniqueViolationError("duplicate key"), {"id": 41}])
    queries = _use_pool(monkeypatch, conn)

    participant_id = await queries.get_or_create_participant("reader-1", "S1", "participant-1")

    assert participant_id == 41
    assert [_is_insert(sql) for sql, _ in conn.fetched] == [False, True, False]
    assert conn.fetched[2][1] == ("participant-1", "S1")


@pytest.mark.asyncio
async def test_other_database_errors_are_not_swallowed(monkeypatch):
    conn = _FakeConn([None, asyncpg.ForeignKeyViolationError("stream missing")])
    queries = _use_pool(monkeypatch, conn)

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await queries.get_or_create_participant("reader-1", "S1", "participant-1")


@pytest.mark.asyncio
async def test_disabled_module_raises(monkeypatch):
    import db.queries.mentorship as queries

    monkeypatch.setattr(queries, "get_mentorship_pool", AsyncMock(return_value=None))

    with pytest.raises(RuntimeError):
        await queries.get_or_create_participant("reader-1", "S1", "participant-1")
