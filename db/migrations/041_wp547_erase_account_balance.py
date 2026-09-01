"""
Migration 041: WP-547 GDPR balance erasure — erase_account_balance(UUID).

Follow-up to migration 040 (protected burn path). `profile.py::_delete_all_user_data`
still issued a direct `DELETE FROM public.point_balances` through the rewards pool,
which now authenticates as `points_redeemer` — a role deliberately denied any direct
write on `point_balances` (migration 040 postflight). This migration adds a narrow
SECURITY DEFINER escape hatch for exactly that one GDPR case, owned by the same
`rewards_points_engine_owner` role as `apply_confirmed_burn_v1`.

Concurrency (found in peer-session review, not present in the original 31.08
protected-path design): erasure takes the SAME account advisory lock as
`apply_confirmed_burn_v1` (`wp547:account:<uuid>`, namespace 0). Without it, a GDPR
delete could interleave with a concurrent burn on the same account — the burn's
row lock on `point_balances` alone gives no cross-statement ordering guarantee
against a delete racing in from a separate transaction.

Grant gap (found in independent review, not present in the peer-session contract):
`rewards_points_engine_owner` has never been granted any privilege on
`point_balances` — that grant lives in the grant/projection cutover
(`wp547-systemic-fix-design.sql` Part 0), which is a separate, not-yet-started
cutover (see cutover-runbook.md). Without an explicit DELETE (and SELECT for the
WHERE clause) grant here, `erase_account_balance` deploys "successfully" but fails
with `permission denied for table point_balances` on its first real call. This
migration grants exactly those two privileges — narrower than the full grant-path
Part 0 (no INSERT/UPDATE), scoped to what this one function needs.

Manual run:
    REWARDS_URL=<dsn as neondb_owner/db-owner role> python -m db.migrations.041_wp547_erase_account_balance
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ERASE_BALANCE_SQL = """
BEGIN;

-- erase_account_balance runs SECURITY DEFINER as rewards_points_engine_owner, which
-- (as of this migration) has no privilege on point_balances at all — the grant lives
-- in the separate, not-yet-started grant/projection cutover. Grant exactly what this
-- one function needs: SELECT for its own WHERE clause, DELETE to actually erase.
-- Idempotent — a repeat GRANT is a no-op, safe if the grant-path cutover later adds
-- the same (or broader) privileges independently.
GRANT SELECT (account_id), DELETE ON public.point_balances
    TO rewards_points_engine_owner;

CREATE OR REPLACE FUNCTION public.erase_account_balance(
    p_account_id UUID
) RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $function$
DECLARE
    v_deleted INTEGER;
BEGIN
    IF p_account_id IS NULL THEN
        RAISE EXCEPTION 'WP-547 erase: account_id is required'
            USING ERRCODE = '22004';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('wp547:account:' || p_account_id::TEXT, 0)
    );

    DELETE FROM public.point_balances WHERE account_id = p_account_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$function$;

REVOKE ALL ON FUNCTION public.erase_account_balance(UUID)
    FROM PUBLIC, points_redeemer, projection_writer_rewards CASCADE;
GRANT EXECUTE ON FUNCTION public.erase_account_balance(UUID)
    TO rewards_points_engine_owner, points_redeemer;
ALTER FUNCTION public.erase_account_balance(UUID)
    OWNER TO rewards_points_engine_owner;

DO $erase_privilege_postflight$
BEGIN
    IF has_table_privilege(
        'points_redeemer', 'public.point_balances', 'DELETE'
    ) THEN
        RAISE EXCEPTION 'WP-547 erase postflight: bot can delete point_balances directly';
    END IF;

    IF NOT has_function_privilege(
        'points_redeemer',
        'public.erase_account_balance(uuid)',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'WP-547 erase postflight: bot cannot call erase_account_balance';
    END IF;

    -- Catches the class of bug found in independent review: postflight checked
    -- only the CALLER's (points_redeemer) privileges, never the SECURITY DEFINER
    -- owner's own ability to actually run its DELETE — the exact gap that made
    -- this "successfully applied" migration fail on its first real call.
    IF NOT has_table_privilege(
        'rewards_points_engine_owner', 'public.point_balances', 'DELETE'
    ) THEN
        RAISE EXCEPTION 'WP-547 erase postflight: owner cannot delete point_balances — erase_account_balance would fail at runtime';
    END IF;
END
$erase_privilege_postflight$;

COMMIT;
"""


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT to_regprocedure('public.erase_account_balance(uuid)') IS NOT NULL"
        )
        if exists:
            return False

    async with pool.acquire() as conn:
        await conn.execute(ERASE_BALANCE_SQL)
    return True


if __name__ == "__main__":
    dsn = os.environ.get("REWARDS_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("Error: REWARDS_URL not set", file=sys.stderr)
        sys.exit(1)

    async def run():
        pool = await asyncpg.create_pool(dsn)
        created = await migrate_if_needed(pool)
        print(f"Migration 041: {'erase_account_balance applied' if created else 'already applied (function exists)'}")
        await pool.close()

    asyncio.run(run())
