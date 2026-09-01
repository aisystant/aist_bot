"""
Migration 042: WP-547 baseline point_balances grants for rewards_points_engine_owner.

Found by independent code review of migration 041 (GDPR erase_account_balance):
`rewards_points_engine_owner` — the SECURITY DEFINER owner of BOTH
`apply_confirmed_burn_v1` (migration 040, already live in production since this
morning) and `erase_account_balance` (migration 041) — has never been granted any
privilege at all on `public.point_balances`. Confirmed live (2026-09-01):
`has_table_privilege('rewards_points_engine_owner', 'public.point_balances', ...)`
is false for SELECT/UPDATE/DELETE. Migration 040's postflight only checked the
CALLING role's (points_redeemer) privileges — never the owner's own ability to run
its own function body. This is the reason `redeemed_events` has almost no rows
(4 total): nobody has actually completed a real burn since the cutover, so this
gap never fired. The first real point redemption WILL fail with
"permission denied for table point_balances" until this migration runs.

Also found live (2026-09-01, `_refresh_subscribers_snapshot` startup job):
`points_redeemer` lacks SELECT on `public.v_subscribers_snapshot_health` — a plain
view is still its own ACL object even though it reads its base table with the
view owner's rights, not the caller's. Fixed alongside since it's the same
"baseline grant gap in the burn-path cutover" root cause.

Full systematic audit (peer session with Codex, 2026-09-01, `MC-sessions:2026-09/01/
2026-09-01-16-wp547-owner-baseline-grants/`) confirmed no third gap: every other
SELECT/UPDATE/INSERT/DELETE the two live SECURITY DEFINER functions perform against
`redeemed_events`/`redeemed_events_audit`/its sequence already has the privilege it
needs; both functions have `search_path` pinned; RLS is off on all three tables
(nothing to bypass).

Postflight does a REAL invocation of `apply_confirmed_burn_v1` against a sentinel
fixture (account_id `00000000-0000-0000-0000-000000000042`) inside a PL/pgSQL
sub-transaction that always rolls itself back via a marker exception — the GRANT
statements above it are outside that sub-transaction and commit normally. This
catches what migration 040's ACL-only postflight missed: the owner's grants
existing is not the same as the owner's function actually running end to end.

Manual run:
    REWARDS_URL=<dsn as neondb_owner/db-owner role> python -m db.migrations.042_wp547_owner_baseline_grants
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

OWNER_BASELINE_GRANTS_SQL = """
BEGIN;

GRANT SELECT (account_id, points), UPDATE (points, last_updated)
    ON public.point_balances
    TO rewards_points_engine_owner;

GRANT SELECT ON public.v_subscribers_snapshot_health TO points_redeemer;

DO $postflight$
DECLARE
    v_sentinel_account UUID := '00000000-0000-0000-0000-000000000042';
    v_sentinel_payment TEXT := 'wp547-042-postflight-sentinel';
    v_result TEXT;
    v_points_before NUMERIC;
    v_points_after NUMERIC;
BEGIN
    IF EXISTS (SELECT 1 FROM public.point_balances WHERE account_id = v_sentinel_account)
       OR EXISTS (SELECT 1 FROM public.redeemed_events WHERE payment_id = v_sentinel_payment)
    THEN
        RAISE EXCEPTION '042 postflight: sentinel account/payment_id already exists — pick a different sentinel';
    END IF;

    BEGIN
        INSERT INTO public.point_balances (account_id, points, last_event_id)
        VALUES (v_sentinel_account, 100, 0);

        v_points_before := 100;

        -- trg_redeemed_events_apply_guard (migration 040) forbids opting into
        -- balance_apply_eligible on INSERT — only a reserved-to-confirmed
        -- UPDATE may do that, matching how redeem.py's real reserve/confirm
        -- flow works. Found live by this migration's own dry run.
        INSERT INTO public.redeemed_events (
            payment_id, account_id, points_amount, discount_rub,
            qualification_snapshot, daily_cap_snapshot, payment_source,
            purpose, reserved_at, status
        ) VALUES (
            v_sentinel_payment, v_sentinel_account, 30, 0,
            '042-postflight-sentinel', 0, 'manual',
            'postflight', clock_timestamp(), 'reserved'
        );

        UPDATE public.redeemed_events
        SET status = 'confirmed', confirmed_at = clock_timestamp(), balance_apply_eligible = TRUE
        WHERE payment_id = v_sentinel_payment;

        -- apply_confirmed_burn_v1 has EXECUTE only for rewards_points_engine_owner/
        -- projection_writer_rewards/points_redeemer (migration 040) — the
        -- operator role running this migration is deliberately none of those.
        -- SET LOCAL ROLE (scoped to this transaction) to the real owner to
        -- invoke it exactly as SECURITY DEFINER production callers would.
        -- Migration 040 already granted this operator SET-only membership
        -- (`GRANT rewards_points_engine_owner TO CURRENT_USER WITH SET TRUE`).
        SET LOCAL ROLE rewards_points_engine_owner;
        v_result := public.apply_confirmed_burn_v1(v_sentinel_payment);
        RESET ROLE;
        IF v_result IS DISTINCT FROM 'applied' THEN
            RAISE EXCEPTION '042 postflight: apply_confirmed_burn_v1 returned %, expected applied', v_result;
        END IF;

        SELECT points INTO v_points_after
        FROM public.point_balances WHERE account_id = v_sentinel_account;
        IF v_points_after IS DISTINCT FROM 70 THEN
            RAISE EXCEPTION '042 postflight: point_balances after burn = %, expected 70', v_points_after;
        END IF;

        -- Sub-transaction rollback marker: undoes the sentinel INSERTs/UPDATE/
        -- audit rows above without touching the GRANT statements, which live
        -- outside this DO block in the same outer transaction and commit below.
        RAISE EXCEPTION USING ERRCODE = 'Z0042', MESSAGE = '042 postflight rollback marker';
    EXCEPTION
        WHEN SQLSTATE 'Z0042' THEN
            NULL;
    END;
END
$postflight$;

COMMIT;
"""


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        already_granted = await conn.fetchval(
            "SELECT has_table_privilege('rewards_points_engine_owner', "
            "'public.point_balances', 'UPDATE')"
        )
        if already_granted:
            return False

    async with pool.acquire() as conn:
        await conn.execute(OWNER_BASELINE_GRANTS_SQL)
    return True


if __name__ == "__main__":
    dsn = os.environ.get("REWARDS_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("Error: REWARDS_URL not set", file=sys.stderr)
        sys.exit(1)

    async def run():
        pool = await asyncpg.create_pool(dsn)
        created = await migrate_if_needed(pool)
        print(f"Migration 042: {'owner baseline grants applied + live postflight passed' if created else 'already applied'}")
        await pool.close()

    asyncio.run(run())
