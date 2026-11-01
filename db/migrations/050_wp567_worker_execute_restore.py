"""
Migration 050: WP-567 -- restore EXECUTE on compute_effective_amount_v4 for
neondb_owner, the role the multi-domain-projection-worker actually writes
rewards as.

Incident (found 2026-10-27): after migration 048 moved the function's
ownership to rewards_points_engine_owner (applied 2026-10-10), the last
real accrual in rewards.applied_events is 2026-10-10 06:44 UTC and none
follow, for every account. Worker log: "permission denied for function
compute_effective_amount_v4" on the INSERT into point_balances, then three
failures open the worker's circuit breaker and the rest of the events are
skipped.

Cause: for rewards the worker connects as neondb_owner by an accepted
decision of 2026-09-03 (db.py / README of multi-domain-projection-worker:
a dedicated writer role crash-looped the worker for 41 hours and the owner
was accepted as the writer). ALTER FUNCTION ... OWNER TO rewrites the old
owner's ACL entry onto the new owner, so neondb_owner=X became
rewards_points_engine_owner=X and neondb_owner lost EXECUTE. 048's
postflight checked projection_writer_rewards (which is not the live caller)
and explicitly did not assert neondb_owner ("Not asserted" note there).

This migration only restores the pre-048 state: an explicit EXECUTE grant to
neondb_owner. It widens nothing: neondb_owner already owns point_balances
and applied_events and can write them directly. Moving the worker to a
narrower role is a separate cutover (LegacyPortGate), not done here.

The postflight runs the function as the CURRENT role (neondb_owner), i.e.
the role the worker really uses -- the check 048 lacked -- on both branches
(referral_attributed early return; ai_chat general branch that calls the two
helpers), then removes the sentinel rows.

Replaying the events missed since 2026-10-10 is NOT done here: the worker's
cursor already moved past most of them, so they need a separate, batched
backfill (see the WP-567 card).

Rollback: under rewards_points_engine_owner,
    REVOKE EXECUTE ON FUNCTION public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone) FROM neondb_owner;

Manual run (needs a rewards-DB owner DSN):
    REWARDS_URL=<dsn as neondb_owner> python -m db.migrations.050_wp567_worker_execute_restore
"""

import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

FUNCTION_IDENTITY = "public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)"

MIGRATION_SQL = """
BEGIN;

DO $preflight$
DECLARE
    v_owner regrole;
BEGIN
    SELECT proowner INTO v_owner
    FROM pg_proc
    WHERE oid = 'public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)'::regprocedure;

    IF v_owner::text <> 'rewards_points_engine_owner' THEN
        RAISE EXCEPTION '050 preflight: owner is %, expected rewards_points_engine_owner (048 not applied or changed)', v_owner;
    END IF;

    IF current_user <> 'neondb_owner' THEN
        RAISE EXCEPTION '050 preflight: run as neondb_owner (the worker role), not %', current_user;
    END IF;

    IF NOT pg_has_role(current_user, 'rewards_points_engine_owner', 'SET') THEN
        RAISE EXCEPTION '050 preflight: % cannot SET ROLE rewards_points_engine_owner, needed to grant on its function', current_user;
    END IF;

    IF has_function_privilege('neondb_owner', 'public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)', 'EXECUTE') THEN
        RAISE EXCEPTION '050 preflight: neondb_owner already has EXECUTE -- nothing to restore';
    END IF;
END
$preflight$;

SET LOCAL ROLE rewards_points_engine_owner;
GRANT EXECUTE ON FUNCTION public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone) TO neondb_owner;
RESET ROLE;

DO $postflight$
DECLARE
    v_sentinel_account_referral UUID := '00000000-0000-0000-0000-000000000050';
    v_sentinel_event_referral BIGINT := -50050001;
    v_sentinel_account_general UUID := '00000000-0000-0000-0000-000000000051';
    v_sentinel_event_general BIGINT := -50050002;
    v_result_referral NUMERIC;
    v_result_general NUMERIC;
BEGIN
    IF NOT has_function_privilege('neondb_owner', 'public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)', 'EXECUTE') THEN
        RAISE EXCEPTION '050 postflight: grant did not take';
    END IF;

    -- The grant must not have leaked beyond what existed before 048.
    IF NOT has_function_privilege('projection_writer_rewards', 'public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)', 'EXECUTE') THEN
        RAISE EXCEPTION '050 postflight: projection_writer_rewards lost EXECUTE';
    END IF;

    IF EXISTS (SELECT 1 FROM public.applied_events WHERE event_id IN (v_sentinel_event_referral, v_sentinel_event_general)) THEN
        RAISE EXCEPTION '050 postflight: sentinel event_id already exists -- pick different sentinels';
    END IF;

    -- Called as the current role (neondb_owner) on purpose: this is the role
    -- the worker uses. 048 called it only as other roles and missed this.
    v_result_referral := public.compute_effective_amount_v4(
        v_sentinel_account_referral, v_sentinel_event_referral, 'referral_attributed',
        '{}'::jsonb, clock_timestamp()
    );
    IF v_result_referral IS NULL THEN
        RAISE EXCEPTION '050 postflight: referral_attributed branch returned NULL';
    END IF;

    v_result_general := public.compute_effective_amount_v4(
        v_sentinel_account_general, v_sentinel_event_general, 'ai_chat',
        '{}'::jsonb, clock_timestamp()
    );
    IF v_result_general IS NULL THEN
        RAISE EXCEPTION '050 postflight: general branch (ai_chat) returned NULL, expected a numeric delta';
    END IF;

    DELETE FROM public.applied_events WHERE event_id IN (v_sentinel_event_referral, v_sentinel_event_general);
    DELETE FROM public.point_balances WHERE last_event_id IN (v_sentinel_event_referral, v_sentinel_event_general);
END
$postflight$;

COMMIT;
"""


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        already_granted = await conn.fetchval(
            "SELECT has_function_privilege('neondb_owner', $1::text::regprocedure, 'EXECUTE')",
            FUNCTION_IDENTITY,
        )
        if already_granted:
            return False

    async with pool.acquire() as conn:
        await conn.execute(MIGRATION_SQL)
    return True


if __name__ == "__main__":
    dsn = os.environ.get("REWARDS_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("Error: REWARDS_URL not set", file=sys.stderr)
        sys.exit(1)

    async def run():
        pool = await asyncpg.create_pool(dsn)
        applied = await migrate_if_needed(pool)
        print(f"Migration 050: {'EXECUTE restored for neondb_owner + live postflight passed' if applied else 'already applied'}")
        await pool.close()

    asyncio.run(run())
