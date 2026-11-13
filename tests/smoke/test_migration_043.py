"""WP-568: migration 043 must not re-run its DDL once nudge_receipt exists.

The prod bot role has no CREATE on schema development. PostgreSQL checks that
privilege before IF NOT EXISTS, so re-running the DDL at every startup logged
"Migration 043 NOT applied" even after the owner created the table by hand.
"""

import importlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import asyncpg
import pytest


migration = importlib.import_module("db.migrations.043_wp117_milestone_receipts")


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def make_conn(table_exists: bool) -> AsyncMock:
    conn = AsyncMock()
    conn.fetchval.return_value = table_exists
    # Mirrors prod: any DDL in schema development is refused for the bot role.
    conn.execute.side_effect = asyncpg.InsufficientPrivilegeError(
        "permission denied for schema development"
    )
    return conn


@pytest.mark.asyncio
async def test_existing_table_skips_ddl_without_create_privilege():
    conn = make_conn(table_exists=True)

    assert await migration.migrate_if_needed(FakePool(conn)) is False
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_table_runs_ddl_and_reports_created():
    conn = make_conn(table_exists=False)
    conn.execute.side_effect = None

    assert await migration.migrate_if_needed(FakePool(conn)) is True
    conn.execute.assert_awaited_once_with(migration.DDL)


@pytest.mark.asyncio
async def test_missing_table_without_privilege_still_raises():
    conn = make_conn(table_exists=False)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await migration.migrate_if_needed(FakePool(conn))
