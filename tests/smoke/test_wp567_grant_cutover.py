"""Offline checks for migration 048's exact-signature ownership guard."""

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "db/migrations/048_wp567_grant_cutover.py"
)
SPEC = importlib.util.spec_from_file_location("wp567_grant_cutover", MIGRATION_PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


@pytest.mark.parametrize("owner_already_moved", [True, False])
def test_exact_signature_parameter_controls_ownership_cutover(owner_already_moved):
    pool = MagicMock()
    connection = pool.acquire.return_value.__aenter__.return_value
    connection.fetchval = AsyncMock(return_value=owner_already_moved)
    connection.execute = AsyncMock()

    applied = asyncio.run(migration.migrate_if_needed(pool))

    connection.fetchval.assert_awaited_once_with(
        "SELECT proowner::regrole::text = 'rewards_points_engine_owner' "
        "FROM pg_proc WHERE oid = $1::text::regprocedure",
        "public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)",
    )
    if owner_already_moved:
        assert applied is False
        assert pool.acquire.call_count == 1
        connection.execute.assert_not_awaited()
    else:
        assert applied is True
        assert pool.acquire.call_count == 2
        connection.execute.assert_awaited_once_with(migration.MIGRATION_SQL)
