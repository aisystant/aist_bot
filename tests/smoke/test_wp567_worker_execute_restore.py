"""Offline checks for migration 050: EXECUTE restore for the worker role."""

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "db/migrations/050_wp567_worker_execute_restore.py"
)
SPEC = importlib.util.spec_from_file_location("wp567_worker_execute_restore", MIGRATION_PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


@pytest.mark.parametrize("already_granted", [True, False])
def test_grant_is_applied_only_when_worker_role_lacks_execute(already_granted):
    pool = MagicMock()
    connection = pool.acquire.return_value.__aenter__.return_value
    connection.fetchval = AsyncMock(return_value=already_granted)
    connection.execute = AsyncMock()

    applied = asyncio.run(migration.migrate_if_needed(pool))

    connection.fetchval.assert_awaited_once_with(
        "SELECT has_function_privilege('neondb_owner', $1::text::regprocedure, 'EXECUTE')",
        "public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)",
    )
    if already_granted:
        assert applied is False
        connection.execute.assert_not_awaited()
    else:
        assert applied is True
        connection.execute.assert_awaited_once_with(migration.MIGRATION_SQL)


def test_sql_grants_only_to_neondb_owner_and_checks_both_branches():
    sql = migration.MIGRATION_SQL

    assert sql.count("GRANT EXECUTE") == 1
    assert "TO neondb_owner;" in sql
    assert "TO PUBLIC" not in sql
    assert "'referral_attributed'" in sql and "'ai_chat'" in sql
    assert sql.index("RESET ROLE;") < sql.index("$postflight$")
