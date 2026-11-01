"""WP-572: payment readiness must reject the pre-046 workshop schema."""

import asyncio
import importlib
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import asyncpg
import pytest

from db import connection, models


migration = importlib.import_module("db.migrations.046_workshop_payments_product")


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def money_schema(monkeypatch):
    main_schema = {
        "public.workshop_payments": {"product"},
        "public.internship_payment_checks": set(),
    }
    bot_data_schema = {"public.finance_payments": set()}

    def make_connection(schema):
        def fetchval(query, table, *columns):
            if columns:
                assert "pg_catalog.pg_attribute" in query
                assert "NOT attisdropped" in query
                return columns[0] in schema.get(table, set())
            return table if table in schema else None

        return AsyncMock(fetchval=AsyncMock(side_effect=fetchval))

    main_conn = make_connection(main_schema)
    bot_data_conn = make_connection(bot_data_schema)

    async def get_pool():
        return FakePool(main_conn)

    async def get_bot_data_pool():
        return FakePool(bot_data_conn)

    monkeypatch.setattr(connection, "get_pool", get_pool)
    monkeypatch.setattr(connection, "get_bot_data_pool", get_bot_data_pool)
    monkeypatch.setattr(connection, "_send_schema_alert", AsyncMock())
    return main_schema, bot_data_schema, main_conn, bot_data_conn


@pytest.mark.asyncio
async def test_payment_guard_accepts_complete_schema_without_ddl(money_schema):
    _, _, main_conn, bot_data_conn = money_schema

    await connection.verify_money_tables()

    main_conn.execute.assert_not_awaited()
    bot_data_conn.execute.assert_not_awaited()
    connection._send_schema_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_guard_rejects_missing_product_in_runtime_pool(
    money_schema, monkeypatch
):
    main_schema, bot_data_schema, main_conn, bot_data_conn = money_schema
    main_schema["public.workshop_payments"].clear()
    # A complete table in another database must not mask runtime schema drift.
    bot_data_schema["public.workshop_payments"] = {"product"}
    monkeypatch.setenv("SKIP_DB_MIGRATIONS", "true")

    with pytest.raises(
        RuntimeError, match=r"public\.workshop_payments\.product@get_pool"
    ):
        await connection.verify_money_tables()

    connection._send_schema_alert.assert_awaited_once()
    assert "migration 046" in connection._send_schema_alert.call_args.args[0]
    main_conn.execute.assert_not_awaited()
    bot_data_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema_index", "table"),
    [
        (0, "public.workshop_payments"),
        (0, "public.internship_payment_checks"),
        (1, "public.finance_payments"),
    ],
)
async def test_payment_guard_still_rejects_missing_tables(
    money_schema, schema_index, table
):
    del money_schema[schema_index][table]

    with pytest.raises(RuntimeError, match=re.escape(table)):
        await connection.verify_money_tables()


@pytest.mark.asyncio
async def test_payment_guard_does_not_accept_failed_column_query(money_schema):
    main_conn = money_schema[2]
    main_conn.fetchval.side_effect = ["public.workshop_payments", TimeoutError()]

    with pytest.raises(TimeoutError):
        await connection.verify_money_tables()

    main_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_database_workshop_table_has_nullable_product(monkeypatch):
    monkeypatch.setattr(models, "SKIP_DB_MIGRATIONS", False)
    conn = AsyncMock()
    conn.fetchval.return_value = False

    await models.create_tables(FakePool(conn))

    workshop_ddl = [
        call.args[0]
        for call in conn.execute.await_args_list
        if "CREATE TABLE IF NOT EXISTS public.workshop_payments" in call.args[0]
    ]
    assert len(workshop_ddl) == 1
    assert re.search(r"\bproduct\s+TEXT\s*,", workshop_ddl[0])


@pytest.mark.asyncio
async def test_migration_skips_an_existing_product_column():
    conn = AsyncMock()
    conn.fetchval.return_value = True

    assert await migration.migrate_if_needed(FakePool(conn)) is False
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_migration_tolerates_concurrent_owner_runs():
    """Both callers may observe the old schema before either ALTER acquires a lock."""
    column_exists = False
    readers = 0
    both_read = asyncio.Event()

    async def fetchval(query):
        nonlocal readers
        observed = column_exists
        readers += 1
        if readers == 2:
            both_read.set()
        await both_read.wait()
        return observed

    async def execute(query):
        nonlocal column_exists
        if column_exists and "ADD COLUMN IF NOT EXISTS" not in query:
            raise asyncpg.DuplicateColumnError("product already exists")
        column_exists = True

    conn = AsyncMock(
        fetchval=AsyncMock(side_effect=fetchval),
        execute=AsyncMock(side_effect=execute),
    )
    pool = FakePool(conn)

    await asyncio.wait_for(
        asyncio.gather(
            migration.migrate_if_needed(pool), migration.migrate_if_needed(pool)
        ),
        timeout=1,
    )

    assert column_exists
    assert conn.execute.await_count == 2
    assert await migration.migrate_if_needed(pool) is False
