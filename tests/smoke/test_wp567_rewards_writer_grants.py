"""Offline checks for migration 051: grants for the narrow rewards writer role."""

import asyncio
import importlib.util
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "db/migrations/051_wp567_rewards_writer_grants.py"
)
SPEC = importlib.util.spec_from_file_location("wp567_rewards_writer_grants", MIGRATION_PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)

SQL = migration.MIGRATION_SQL


def _pool(missing_before, missing_after=()):
    pool = MagicMock()
    connection = pool.acquire.return_value.__aenter__.return_value
    connection.fetch = AsyncMock(side_effect=[list(missing_before), list(missing_after)])
    connection.execute = AsyncMock()
    return pool, connection


@pytest.mark.parametrize("missing_before", [[], [("public.point_balances", "INSERT", "points")]])
def test_postflight_runs_every_time_and_the_result_is_rechecked(missing_before):
    """Already granted by hand is not proof the role can write: the postflight still runs."""
    pool, connection = _pool(missing_before)

    assert asyncio.run(migration.migrate(pool)) == missing_before

    connection.execute.assert_awaited_once_with(SQL)
    assert connection.fetch.await_count == 2
    sql, tables, privs, columns, role = connection.fetch.await_args.args
    assert sql == migration.MISSING_PRIVILEGES_SQL and role == "projection_writer_rewards"
    rows = set(zip(tables, privs, columns))
    assert ("public.point_balances", "UPDATE", "points") in rows
    assert ("public.applied_events", "SELECT", None) in rows


def test_privileges_still_missing_after_the_run_fail_loudly():
    pool, _ = _pool(
        missing_before=[("public.typing_daily", "SELECT", None)],
        missing_after=[("public.typing_daily", "SELECT", None)],
    )

    with pytest.raises(RuntimeError, match="still missing"):
        asyncio.run(migration.migrate(pool))


def test_writes_are_granted_per_column_and_never_to_earned_or_ceiling():
    grants = re.findall(r"^GRANT (\w+)(?: \(([^)]*)\))? ON (\S+) TO (\S+);$", SQL, re.M)

    assert len(grants) == len(migration.GRANTS)
    assert {role for *_, role in grants} == {"projection_writer_rewards"}
    for priv, columns, table, _ in grants:
        if priv in ("INSERT", "UPDATE"):
            assert columns, f"table-wide {priv} on {table}"
    written = " ".join(columns for _, columns, _, _ in grants)
    for protected in ("earned_total", "historic_bonus_ceiling", "points_earned"):
        assert protected not in written
    assert not any(priv in ("DELETE", "TRUNCATE") for priv, *_ in grants)
    assert "TO PUBLIC" not in SQL


def test_typing_keys_are_inserted_but_never_updated():
    update = re.search(r"^GRANT UPDATE \(([^)]*)\) ON public\.typing_daily", SQL, re.M).group(1)
    assert not {"account_id", "date", "interface"} & set(update.split(", "))


def test_postflight_proves_the_paths_that_broke_before_and_rolls_back():
    postflight = SQL[SQL.index("SET LOCAL ROLE projection_writer_rewards;"):SQL.index("RESET ROLE;")]
    sub_block = postflight[:postflight.index("WHEN SQLSTATE 'P0051'")]

    for proof in (
        "compute_effective_amount_v4(",
        "'referral_attributed'",
        "'ai_chat'",
        "INSERT INTO public.point_balances",
        "INSERT INTO public.typing_daily",
        "compute_typing_points(",
        "INSERT INTO public.first_payment_guard",
    ):
        assert proof in sub_block, proof
    assert sub_block.rindex("RAISE EXCEPTION USING ERRCODE = 'P0051'") > sub_block.rindex("INSERT INTO")
    assert "has_table_privilege(v_table, 'DELETE')" in postflight
    assert "'{}'::jsonb" in postflight  # the f-string escaped the braces


def test_rewards_dsn_is_derived_from_the_reference_one_keeping_host_and_options(monkeypatch):
    monkeypatch.delenv("REWARDS_URL", raising=False)
    monkeypatch.setenv("NEON_REFERENCE_URL", "postgresql://host.example/reference?sslmode=require")

    assert migration._dsn_from_env() == "postgresql://host.example/rewards?sslmode=require"

    monkeypatch.setenv("REWARDS_URL", "postgresql://explicit.example/rewards")
    assert migration._dsn_from_env() == "postgresql://explicit.example/rewards"
