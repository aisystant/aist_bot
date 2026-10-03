"""
WP-578 F9 - contract of db.queries.mentorship.list_reader_streams: an empty list
("not a reader anywhere") and RuntimeError ("module disabled") must stay distinct,
otherwise the command guard cannot tell a healthy bot from a disabled module.
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
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import pytest
from unittest.mock import AsyncMock


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []
        self.fetched = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetch(self, sql, *args):
        self.fetched.append((sql, args))
        return self._rows


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


@pytest.mark.asyncio
async def test_disabled_module_raises_instead_of_returning_empty(monkeypatch):
    import db.queries.mentorship as queries

    monkeypatch.setattr(queries, "get_mentorship_pool", AsyncMock(return_value=None))

    with pytest.raises(RuntimeError):
        await queries.list_reader_streams("account-1")


@pytest.mark.asyncio
async def test_account_without_reader_rows_gets_empty_list(monkeypatch):
    import db.queries.mentorship as queries

    conn = _FakeConn(rows=[])
    monkeypatch.setattr(queries, "get_mentorship_pool", AsyncMock(return_value=_FakePool(conn)))

    assert await queries.list_reader_streams("account-1") == []


@pytest.mark.asyncio
async def test_reader_rows_are_returned_as_stream_role_pairs_under_account_context(monkeypatch):
    import db.queries.mentorship as queries

    conn = _FakeConn(rows=[{"stream_id": "S1-2026.3-T", "role": "mentor"}, {"stream_id": "S2-2026.4-T", "role": "pilot"}])
    monkeypatch.setattr(queries, "get_mentorship_pool", AsyncMock(return_value=_FakePool(conn)))

    result = await queries.list_reader_streams("account-1")

    assert result == [("S1-2026.3-T", "mentor"), ("S2-2026.4-T", "pilot")]
    # the row filter is the application-level perimeter (RLS is dormant): it must use the caller's own account
    assert conn.fetched[0][1] == ("account-1",)
    assert "account_id = $1" in conn.fetched[0][0]
    assert conn.executed[0][1] == ("account-1",)  # set_config(app.current_account_id, ...) ran first
