"""
Migration 051: WP-567 Ф5 step 2 -- grants for projection_writer_rewards, so the
multi-domain-projection-worker can move from neondb_owner to its narrow writer role.

What the role had on 2026-10-28 (read-only catalog check,
multi-domain-projection-worker/scripts/f5_rewards_role_catalog.py): LOGIN,
CONNECT, USAGE on public, EXECUTE on compute_effective_amount_v4 and
apply_confirmed_burn_v1 (both SECURITY DEFINER under rewards_points_engine_owner),
SELECT on redeemed_events -- and no other table privilege. The worker as this
role would fail every accrual the moment it switched, as on 2026-09-02/03.

What the worker runs as the caller (worker code, 2026-10-28):
  point_balances   rule UPSERTs and first payment; columns limited by the worker's
                   ALLOWED_TARGETS (matcher.py): account_id, points, last_event_id, last_updated
  typing_daily     user_typing_tracked UPSERT; ALLOWED_TARGETS: account_id, date, interface,
                   char_count, weighted_chars, events_count, updated_at (keys never updated)
  applied_events   DLQ accrual check, first-payment existence check (read)
  subscribers_snapshot  subscriber gate before typing (read)
  first_payment_guard   INSERT ... ON CONFLICT DO NOTHING RETURNING account_id
SECURITY INVOKER triggers run as the caller too:
  trg_typing_daily_sync_points reads subscribers_snapshot, calls compute_typing_points
  and upserts point_balances (points, last_updated) -> EXECUTE on compute_typing_points.
  trg_point_balances_track_earned only edits NEW (no column privilege needed).
  trg_update_historic_bonus_ceiling fires on applied_events inserts, which happen only
  inside the SECURITY DEFINER function, i.e. as its owner: nothing for the caller.
No RLS on these tables; no lookups of the rewards database in the worker.

Writes are granted per column, so the role cannot set earned_total,
historic_bonus_ceiling or points_earned directly. Reads are table-wide.

The switch itself is not done here: it is the worker's
DATABASE_URL_REWARDS_PROJECTION variable, set by the pilot after this migration and
accepted by a real login as the role (not SET ROLE).

Postflight runs AS projection_writer_rewards (SET LOCAL ROLE) on every run, also
when nothing was missing: compute_effective_amount_v4 on both branches (the call
that broke on 2026-09-02 and 2026-10-10), the worker's point_balances and typing_daily
upserts (insert and update paths, with their triggers), compute_typing_points, the
first-payment guard insert, the reads. Every sentinel write sits in one sub-block that
ends with a deliberate exception, so it is all rolled back, trigger side effects and
applied_events rows included. It then asserts the role has no table-wide write and no
DELETE/TRUNCATE on the tables.

The postflight needs neondb_owner to SET ROLE to the narrow role. On PG16 the creator of a
role gets ADMIN but not SET (the first live run on 2026-10-28 refused exactly there), so the
preflight grants neondb_owner SET without INHERIT when it holds ADMIN and SET is missing.

Rollback (as neondb_owner):
    REVOKE ALL ON public.point_balances, public.typing_daily, public.applied_events,
                  public.subscribers_snapshot, public.first_payment_guard FROM projection_writer_rewards;
    -- and EXECUTE on compute_typing_points if the run printed that it granted it
    REVOKE projection_writer_rewards FROM neondb_owner;  -- only if the run printed that it granted SET

Manual run (needs a rewards-DB owner DSN; without REWARDS_URL it is derived from
NEON_REFERENCE_URL, the preflight still refuses anything but neondb_owner):
    AIST_ENV_ALLOW=NEON_REFERENCE_URL ~/IWE/scripts/with-aist-env.sh \
        .venv/bin/python -m db.migrations.051_wp567_rewards_writer_grants
"""

import asyncio
import os
import sys
from urllib.parse import urlparse, urlunparse

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ROLE = "projection_writer_rewards"

# (privilege, table, columns or None for table-wide): the one list the check and the SQL are built from.
GRANTS = (
    ("SELECT", "public.point_balances", None),
    ("INSERT", "public.point_balances", ("account_id", "points", "last_updated", "last_event_id")),
    ("UPDATE", "public.point_balances", ("points", "last_updated", "last_event_id")),
    ("SELECT", "public.typing_daily", None),
    (
        "INSERT",
        "public.typing_daily",
        ("account_id", "date", "interface", "char_count", "weighted_chars", "events_count", "updated_at"),
    ),
    ("UPDATE", "public.typing_daily", ("char_count", "weighted_chars", "events_count", "updated_at")),
    ("SELECT", "public.applied_events", None),
    ("SELECT", "public.subscribers_snapshot", None),
    ("SELECT", "public.first_payment_guard", None),
    ("INSERT", "public.first_payment_guard", ("account_id", "payment_ref", "occurred_at", "source")),
)

TABLES = tuple(dict.fromkeys(table for _, table, _ in GRANTS))

MISSING_PRIVILEGES_SQL = """
SELECT g.tbl, g.priv, g.col
FROM unnest($1::text[], $2::text[], $3::text[]) AS g(tbl, priv, col)
WHERE NOT CASE WHEN g.col IS NULL THEN has_table_privilege($4::text, g.tbl, g.priv)
               ELSE has_column_privilege($4::text, g.tbl, g.col, g.priv) END
UNION ALL
SELECT p.oid::regprocedure::text, 'EXECUTE', NULL
FROM pg_proc p
WHERE p.proname = 'compute_typing_points' AND p.pronamespace = 'public'::regnamespace
  AND NOT has_function_privilege($4::text, p.oid, 'EXECUTE')
"""


def _grant_statements() -> str:
    lines = []
    for priv, table, columns in GRANTS:
        target = f"{priv} ({', '.join(columns)})" if columns else priv
        lines.append(f"GRANT {target} ON {table} TO {ROLE};")
    return "\n".join(lines)


def _sql_list(items) -> str:
    return ", ".join(f"'{item}'" for item in items)


MIGRATION_SQL = f"""
BEGIN;

DO $preflight$
DECLARE
    v_table TEXT;
BEGIN
    IF current_user <> 'neondb_owner' THEN
        RAISE EXCEPTION '051 preflight: run as neondb_owner (owner of the rewards tables), not %', current_user;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}' AND rolcanlogin AND NOT rolbypassrls) THEN
        RAISE EXCEPTION '051 preflight: {ROLE} missing, cannot log in, or bypasses RLS';
    END IF;

    -- The postflight needs SET ROLE. On PG16 the role's creator holds ADMIN but not SET
    -- (seen on the first live run 2026-10-28), so take SET only, without inheriting anything.
    IF NOT pg_has_role(current_user, '{ROLE}', 'SET') THEN
        IF NOT pg_has_role(current_user, '{ROLE}', 'MEMBER WITH ADMIN OPTION') THEN
            RAISE EXCEPTION '051 preflight: % can neither SET ROLE {ROLE} nor grant itself membership', current_user;
        END IF;
        GRANT {ROLE} TO neondb_owner WITH INHERIT FALSE, SET TRUE;
        RAISE NOTICE '051: granted neondb_owner SET (no INHERIT) on {ROLE}';
    END IF;
    IF NOT pg_has_role(current_user, '{ROLE}', 'SET') THEN
        RAISE EXCEPTION '051 preflight: % still cannot SET ROLE {ROLE}', current_user;
    END IF;

    FOR v_table IN SELECT unnest(ARRAY[{_sql_list(TABLES)}]) LOOP
        IF pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid = v_table::regclass)) <> current_user THEN
            RAISE EXCEPTION '051 preflight: % is not owned by %, cannot grant on it', v_table, current_user;
        END IF;
    END LOOP;
END
$preflight$;

{_grant_statements()}

-- compute_typing_points is called by the SECURITY INVOKER trigger on typing_daily, i.e. as the caller.
DO $typing_points$
DECLARE
    v_fn RECORD;
BEGIN
    FOR v_fn IN
        SELECT p.oid, p.oid::regprocedure::text AS ident, pg_get_userbyid(p.proowner) AS owner
        FROM pg_proc p
        WHERE p.proname = 'compute_typing_points' AND p.pronamespace = 'public'::regnamespace
    LOOP
        IF has_function_privilege('{ROLE}', v_fn.oid, 'EXECUTE') THEN
            CONTINUE;
        END IF;
        IF v_fn.owner <> current_user THEN
            RAISE EXCEPTION '051: {ROLE} lacks EXECUTE on % owned by %, grant it as that owner first', v_fn.ident, v_fn.owner;
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO {ROLE}', v_fn.ident);
        RAISE NOTICE '051: granted EXECUTE on % to {ROLE}', v_fn.ident;
    END LOOP;
    IF NOT FOUND THEN
        RAISE EXCEPTION '051: public.compute_typing_points not found, the typing trigger cannot work';
    END IF;
END
$typing_points$;

SET LOCAL ROLE {ROLE};

DO $postflight$
DECLARE
    v_account UUID := '00000000-0000-0000-0000-000000000510';
    v_event_referral BIGINT := -50051001;
    v_event_general BIGINT := -50051002;
    v_result NUMERIC;
    v_guard UUID;
    v_table TEXT;
BEGIN
    IF current_user <> '{ROLE}' THEN
        RAISE EXCEPTION '051 postflight: running as %, expected {ROLE}', current_user;
    END IF;

    IF EXISTS (SELECT 1 FROM public.applied_events WHERE event_id IN (v_event_referral, v_event_general))
       OR EXISTS (SELECT 1 FROM public.point_balances WHERE account_id = v_account)
       OR EXISTS (SELECT 1 FROM public.first_payment_guard WHERE account_id = v_account) THEN
        RAISE EXCEPTION '051 postflight: sentinel account or events already exist -- pick different sentinels';
    END IF;

    BEGIN
        -- The function the worker calls before every accrual, both branches (as 050).
        v_result := public.compute_effective_amount_v4(
            v_account, v_event_referral, 'referral_attributed', '{{}}'::jsonb, clock_timestamp());
        IF v_result IS NULL THEN
            RAISE EXCEPTION '051 postflight: referral_attributed branch returned NULL';
        END IF;
        v_result := public.compute_effective_amount_v4(
            v_account, v_event_general, 'ai_chat', '{{}}'::jsonb, clock_timestamp());
        IF v_result IS NULL THEN
            RAISE EXCEPTION '051 postflight: general branch (ai_chat) returned NULL';
        END IF;

        -- The worker's point_balances upsert (first_payment.py), insert path then update path.
        FOR i IN 1..2 LOOP
            INSERT INTO public.point_balances (account_id, points, last_updated, last_event_id)
            VALUES (v_account, 0, now(), v_event_general)
            ON CONFLICT (account_id) DO UPDATE
            SET points = public.point_balances.points + EXCLUDED.points,
                last_updated = EXCLUDED.last_updated,
                last_event_id = EXCLUDED.last_event_id;
        END LOOP;

        -- The worker's typing upsert shape (matcher.py, increments), insert then update; fires the
        -- subscriber-check trigger as the caller.
        FOR i IN 1..2 LOOP
            INSERT INTO public.typing_daily
                (account_id, date, interface, char_count, weighted_chars, events_count, updated_at)
            VALUES (v_account, CURRENT_DATE, '051-sentinel', 10, 10, 1, now())
            ON CONFLICT (account_id, date, interface) DO UPDATE
            SET char_count = COALESCE(public.typing_daily.char_count, 0) + EXCLUDED.char_count,
                weighted_chars = COALESCE(public.typing_daily.weighted_chars, 0) + EXCLUDED.weighted_chars,
                events_count = COALESCE(public.typing_daily.events_count, 0) + EXCLUDED.events_count,
                updated_at = EXCLUDED.updated_at;
        END LOOP;
        -- The trigger calls this only for subscribers; the sentinel is not one, so call it directly.
        PERFORM public.compute_typing_points(1000);

        INSERT INTO public.first_payment_guard (account_id, payment_ref, occurred_at, source)
        VALUES (v_account, '051-sentinel', now(), 'live')
        ON CONFLICT (account_id) DO NOTHING
        RETURNING account_id INTO v_guard;
        IF v_guard IS NULL THEN
            RAISE EXCEPTION '051 postflight: first_payment_guard insert returned nothing';
        END IF;

        PERFORM 1 FROM public.subscribers_snapshot WHERE account_id = v_account;
        PERFORM 1 FROM public.redeemed_events LIMIT 0;

        RAISE EXCEPTION USING ERRCODE = 'P0051', MESSAGE = '051 sentinel rollback';
    EXCEPTION
        WHEN SQLSTATE 'P0051' THEN
            NULL;  -- expected: every sentinel write above, trigger effects included, is undone
    END;

    -- Nothing wider than the column grants: no table-wide write, no DELETE/TRUNCATE.
    FOR v_table IN SELECT unnest(ARRAY[{_sql_list(TABLES)}]) LOOP
        IF has_table_privilege(v_table, 'INSERT') OR has_table_privilege(v_table, 'UPDATE')
           OR has_table_privilege(v_table, 'DELETE') OR has_table_privilege(v_table, 'TRUNCATE') THEN
            RAISE EXCEPTION '051 postflight: {ROLE} has a table-wide write on %, expected column grants only', v_table;
        END IF;
    END LOOP;
END
$postflight$;

RESET ROLE;

COMMIT;
"""


def _grant_rows() -> tuple[list[str], list[str], list[str | None]]:
    rows = [
        (table, priv, column)
        for priv, table, columns in GRANTS
        for column in (columns or (None,))
    ]
    return [t for t, _, _ in rows], [p for _, p, _ in rows], [c for _, _, c in rows]


async def missing_privileges(conn: asyncpg.Connection) -> list[tuple]:
    tables, privs, columns = _grant_rows()
    rows = await conn.fetch(MISSING_PRIVILEGES_SQL, tables, privs, columns, ROLE)
    return [tuple(row) for row in rows]


async def migrate(pool: asyncpg.Pool) -> list[tuple]:
    """Grant and prove; return what was missing before. GRANT is idempotent, so the
    postflight runs every time, also when nothing was missing."""
    async with pool.acquire() as conn:
        missing_before = await missing_privileges(conn)
        await conn.execute(MIGRATION_SQL)
        left = await missing_privileges(conn)
    if left:
        raise RuntimeError(f"051: privileges still missing after the migration: {left}")
    return missing_before


def _dsn_from_env() -> str | None:
    """REWARDS_URL, or the rewards database next to NEON_REFERENCE_URL (same owner credentials),
    so the pilot never pastes a connection string on the command line."""
    if os.environ.get("REWARDS_URL"):
        return os.environ["REWARDS_URL"]
    reference = os.environ.get("NEON_REFERENCE_URL")
    if reference:
        return urlunparse(urlparse(reference)._replace(path="/rewards"))
    return None


if __name__ == "__main__":
    dsn = _dsn_from_env()
    if not dsn:
        print("Error: set REWARDS_URL or NEON_REFERENCE_URL", file=sys.stderr)
        sys.exit(1)

    async def run():
        pool = await asyncpg.create_pool(dsn)
        try:
            missing_before = await migrate(pool)
        finally:
            await pool.close()
        print(f"Migration 051: {len(missing_before)} privileges were missing; grants in place + postflight as {ROLE} passed")

    asyncio.run(run())
