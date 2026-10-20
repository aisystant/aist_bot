"""
Migration 047: WP-567 -- teach compute_effective_amount_v4 to write external_id
for new rows (dual-write side of the applied_events semantic-key protection).

Context: two peer sessions on 2026-09-11 (tsekh-1 with Codex, then the Mac with
Kimi+Codex) added `external_id`/`source_event_hash` columns to `applied_events`,
backfilled 603458/603885 historical rows from `_foreign_learning.domain_event`,
verified zero duplicates across the full history, and built
`idx_applied_events_external_id_unique` (CONCURRENTLY, valid) on top of it.

Codex's reviewed plan (session `2026-09-11-21-rp567-zashchita-balansa-ballov`,
round 1) required dual-write BEFORE backfill, precisely because a plain unique
index never compares NULLs against each other -- a row written after the
index exists but still lacking `external_id` is invisible to it. The actual
execution did backfill+index first and left dual-write for later, which a
cold-review subagent (that session's own turn 3.6.2-equivalent) and an
independent parallel session both flagged: today, new grants still get
`external_id = NULL`, so the constraint protects only the pre-2026-09-11
history, not a new WP-547-style double-grant happening right now. This
migration is that missing piece.

What it does:
  1. Grants the CURRENT owner of `compute_effective_amount_v4` (looked up
     dynamically -- Ф1 has not cut over ownership yet, so this is
     `neondb_owner` today, per WP-567's own findings, but may not stay that
     way) USAGE on schema `_foreign_learning` and SELECT on
     `_foreign_learning.domain_event`. The function is SECURITY DEFINER, so
     the FDW lookup inside it runs under the OWNER's identity, not the
     caller's (`projection_writer_rewards`) -- granting the caller directly,
     as an earlier session's live probe suggested was the gap, would not
     actually be exercised by this code path and would just be an unused
     grant sitting on a role that does not need it.
  2. CREATE OR REPLACE `compute_effective_amount_v4` with one addition: right
     after the existing event-id dedup check, look up
     `_foreign_learning.domain_event.external_id` for this `event_id` and
     carry it into both `applied_events` INSERT statements (the
     `referral_attributed` fixed-amount branch and the general branch). The
     lookup is wrapped in its own BEGIN/EXCEPTION -- a network hiccup or a
     missing FDW mapping must degrade to `external_id = NULL` (today's
     behavior) and log a WARNING, not block real point crediting. This is a
     defense layer, not the primary write path; the primary write path must
     never depend on the foreign server being reachable.
  3. Each of the two `applied_events` INSERTs is wrapped in its own
     BEGIN/EXCEPTION WHEN unique_violation: a collision on the (separate,
     already-live) `external_id` unique index means two different
     `event_id`s claim the same real-world fact -- that must not abort the
     whole call as an unhandled Postgres error, it should behave like the
     existing "already applied" dedup a few lines above it (log + RETURN 0).
  4. The rest of the formula (quals/streak/caps, the referral branch, the
     ineligible-credit routing) is untouched -- copied verbatim from
     `wp547-systemic-fix-design.sql` Part 2 with only the two INSERT column
     lists, the new lookup block, and the two new exception handlers added.

Independent cold-review (Agent tool, 2026-09-12) before this was applied
anywhere found one Critical and two High findings, both fixed in this file
before it was committed:
  - Critical: the original lookup's `WHEN OTHERS` does not catch
    `query_canceled` (Postgres deliberately excludes it from "OTHERS"), and
    no `statement_timeout` was set -- a hung or slow foreign server would
    have aborted real point crediting instead of degrading to NULL, which
    is the exact opposite of what this defense layer is for. Fixed: a 2s
    `statement_timeout` scoped around the lookup (via `set_config(...,
    true)`, restored afterward) plus an explicit `WHEN query_canceled` arm.
  - High: the INSERTs only had `ON CONFLICT (event_id) DO NOTHING`, which
    does not cover the separate `external_id` unique index -- a collision
    there would have raised an uncaught `unique_violation` and aborted a
    real user's crediting transaction. Fixed with item 3 above.
  - High: the automated postflight only ever exercised the "no matching
    domain_event row" path (which degrades to NULL even without any
    EXCEPTION handling, since a plain `SELECT ... INTO` does not raise on
    zero rows) -- it never forced an actual FDW error, so the
    `query_canceled`/`WHEN OTHERS` arms above are verified by code reading,
    not by a live automated test. Deliberately not fixed with a synthetic
    failure injection: forcing a realistic FDW timeout/error safely inside
    this migration's own transaction was judged more likely to introduce
    its own bug than to catch one. Recommended manual check after applying:
    watch for `WP-567: external_id lookup` WARNINGs in Postgres logs after
    the first real new event, and confirm point crediting still succeeded
    for it.

Known, deliberately out-of-scope caveat (found while writing this, not
fixed here): `domain_event`'s own uniqueness is the COMPOSITE
`UNIQUE (source, external_id)`
(`neon-migrations/mvp/002-learning-schema.sql`), not `external_id` alone.
The unique index already live on `applied_events` is single-column
(`external_id`). If two different event sources ever produced the same
`external_id` string, the INSERT would hit the `unique_violation` handler
above and the second event would be dropped as a probable duplicate even
though it may be a distinct, legitimate fact from a different source --
not a silent data-loss risk (it is logged via WARNING and the caller gets
a clean `0`, not a crash), but a real precision gap in what counts as "the
same grant". In practice every event_type reaching this function today
comes from exactly one source, so the practical risk is low -- but this is
a real gap in the semantic key's precision, not an oversight to silently
patch inside a dual-write migration. Revisit if a second event source is
ever wired into the same event_type space; would require adding a `source`
column to `applied_events` and rebuilding the index as a composite one.

NOT applied to production by the agent that wrote this file -- prepared for
review only (pilot instruction, 2026-09-12: "делай и проверяй субагентом").
Whoever runs this manually needs REWARDS_URL with rights to ALTER the
function and GRANT on its owner role.

Manual run:
    REWARDS_URL=<dsn as neondb_owner/db-owner role> python -m db.migrations.047_wp567_dual_write_external_id
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Marker comment inside the function body -- idempotency check greps for it
# via pg_get_functiondef instead of re-deriving "already applied" from
# grants alone (CREATE OR REPLACE is itself idempotent, but re-running the
# live postflight against a SECURITY DEFINER money function on every
# no-op invocation is not something to do by accident).
DUAL_WRITE_MARKER = "wp567_dual_write_external_id"

MIGRATION_SQL = """
BEGIN;

DO $grant_owner$
DECLARE
    v_owner regrole;
BEGIN
    SELECT proowner::regrole INTO v_owner
    FROM pg_proc
    WHERE proname = 'compute_effective_amount_v4'
      AND pronamespace = 'public'::regnamespace;

    IF v_owner IS NULL THEN
        RAISE EXCEPTION '047: compute_effective_amount_v4 not found in public schema';
    END IF;

    EXECUTE format('GRANT USAGE ON SCHEMA _foreign_learning TO %I', v_owner);
    EXECUTE format('GRANT SELECT ON _foreign_learning.domain_event TO %I', v_owner);
END
$grant_owner$;

CREATE OR REPLACE FUNCTION public.compute_effective_amount_v4(
    p_account_id uuid, p_event_id bigint, p_event_type text,
    p_payload jsonb, p_ingested_at timestamp with time zone
) RETURNS numeric
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path = pg_catalog, public, pg_temp
 SET timezone = 'UTC'
AS $function$
-- wp567_dual_write_external_id
DECLARE
    -- rule columns
    v_rule_id           UUID;
    v_amount            NUMERIC;
    v_effort_minutes    INTEGER;
    v_is_marker         BOOLEAN;
    v_max_per_day       INTEGER;
    v_group_mult        INTEGER;
    v_rarity_mult       NUMERIC;
    v_streak_eligible   BOOLEAN;

    -- config
    v_K                 INTEGER := 10;
    v_use_v4            BOOLEAN := FALSE;

    -- qualification (12-level unified)
    v_level             INT;
    v_stage             INT;
    v_qual_mult         NUMERIC := 1.0;
    v_action_cap        NUMERIC := 200.0;

    -- streak
    v_streak_mult       NUMERIC := 1.0;

    -- effort / base
    v_effort_factor     NUMERIC;
    v_base              NUMERIC := 0;
    v_raw               NUMERIC;
    v_effective         NUMERIC;
    v_cap_truncated     BOOLEAN := FALSE;

    -- daily cap
    v_daily_total_cap   NUMERIC;
    v_today_total       NUMERIC;
    v_remaining_total   NUMERIC;

    -- bonuses eligibility (stage >= 2 = Практикующий)
    v_bonuses_eligible  BOOLEAN := TRUE;
    v_max_per_day_reached BOOLEAN := FALSE;

    -- WP-547 forward-fix: результат points-инкремента для внешнего UPSERT
    v_points_delta      NUMERIC;
    v_claimed_event     BIGINT;

    -- WP-567 Ф2 dual-write: best-effort semantic key for the new-duplicate
    -- protection. NULL on any lookup failure -- must never block crediting.
    v_external_id       TEXT;
    v_prior_stmt_timeout TEXT;
BEGIN
    IF p_account_id IS NULL OR p_event_id IS NULL THEN
        RAISE EXCEPTION 'WP-547: account_id and event_id are required'
            USING ERRCODE = '22004';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('wp547:event:' || p_event_id::TEXT, 0)
    );

    IF EXISTS (SELECT 1 FROM public.applied_events WHERE event_id = p_event_id) THEN
        RETURN 0;
    END IF;

    -- WP-567 Ф2 dual-write: look up the source-of-truth semantic key for this
    -- event. Any failure here (FDW unreachable, mapping missing, remote row
    -- absent, or a slow/hung foreign server) degrades to NULL and a WARNING
    -- -- exactly today's behavior -- rather than failing point crediting on
    -- a defense-layer dependency. A bounded statement_timeout is required
    -- for this: WHEN OTHERS does not catch query_canceled (Postgres carves
    -- it out of "OTHERS" on purpose), so without an explicit timeout AND an
    -- explicit WHEN query_canceled arm, a hung foreign server would abort
    -- this whole crediting call instead of degrading -- found by cold review
    -- (2026-09-12), the exact failure mode this block exists to prevent.
    v_prior_stmt_timeout := current_setting('statement_timeout');
    BEGIN
        PERFORM set_config('statement_timeout', '2000', true);
        SELECT external_id INTO v_external_id
        FROM _foreign_learning.domain_event
        WHERE id = p_event_id;
    EXCEPTION
        WHEN query_canceled THEN
            v_external_id := NULL;
            RAISE WARNING 'WP-567: external_id lookup timed out (2s) for event_id=%', p_event_id;
        WHEN OTHERS THEN
            v_external_id := NULL;
            RAISE WARNING 'WP-567: external_id lookup failed for event_id=%: %', p_event_id, SQLERRM;
    END;
    -- set_config(..., true) is SET LOCAL semantics: it does not revert on
    -- its own when the block above exits without an exception, so restore
    -- the caller's timeout explicitly before the rest of this function runs
    -- (or before returning control to callers that share this transaction).
    PERFORM set_config('statement_timeout', v_prior_stmt_timeout, true);

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('wp547:account:' || p_account_id::TEXT, 0)
    );

    -- Флаг формулы
    SELECT use_v4_formula, COALESCE(K, 10)
    INTO v_use_v4, v_K
    FROM _foreign_reference.loyalty_pool_config
    WHERE valid_to IS NULL
    LIMIT 1;

    IF NOT COALESCE(v_use_v4, FALSE) THEN
        RETURN 0;
    END IF;

    SELECT rule_id, amount, effort_minutes, is_marker, max_per_day, group_mult, rarity_mult, streak_eligible
    INTO v_rule_id, v_amount, v_effort_minutes, v_is_marker, v_max_per_day, v_group_mult, v_rarity_mult, v_streak_eligible
    FROM _foreign_reference.reward_rules
    WHERE trigger_event = p_event_type
      AND reward_kind = 'points'
      AND valid_from <= p_ingested_at
      AND (valid_to IS NULL OR valid_to > p_ingested_at)
      AND (match_condition IS NULL OR match_condition <@ p_payload)
    ORDER BY valid_from DESC
    LIMIT 1;

    IF v_rule_id IS NULL THEN
        RETURN 0;
    END IF;

    IF v_max_per_day IS NOT NULL AND v_max_per_day > 0 THEN
        DECLARE v_today_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO v_today_count
            FROM public.applied_events
            WHERE account_id = p_account_id
              AND event_type = p_event_type
              AND DATE(applied_at) = DATE(p_ingested_at);
            v_max_per_day_reached := v_today_count >= v_max_per_day;
        END;
    END IF;

    IF p_event_type = 'referral_attributed' THEN
        v_effective := ROUND(COALESCE(v_amount, 0), 2);
        IF v_max_per_day_reached THEN
            v_effective := 0;
            v_cap_truncated := TRUE;
        END IF;

        -- WP-567: external_id carries its own unique index (partial, WHERE
        -- NOT NULL). ON CONFLICT (event_id) only dedups the technical key --
        -- a semantic-key collision here means two different event_ids claim
        -- the same real-world fact, which must not silently pass through as
        -- an unhandled unique_violation aborting the whole crediting call.
        -- Treat it exactly like the existing event_id dedup above: log and
        -- return 0 (no delta), the same "already applied, no-op" contract
        -- the rest of this function already uses.
        BEGIN
            INSERT INTO public.applied_events (
                event_id, account_id, event_type, base_amount,
                dom_mult, qual_mult, streak_mult, daily_cap,
                raw_amount, effective, cap_truncated, bonuses_eligible,
                applied_at, payload_snapshot, rule_id, external_id
            ) VALUES (
                p_event_id, p_account_id, p_event_type, v_effective,
                1.0, 1.0, 1.0, v_effective,
                v_effective, v_effective, v_cap_truncated, TRUE,
                p_ingested_at,
                jsonb_build_object(
                    'fixed_amount', TRUE,
                    'max_per_day_reached', v_max_per_day_reached,
                    'wp', 473
                ),
                v_rule_id, v_external_id
            )
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id INTO v_claimed_event;
        EXCEPTION WHEN unique_violation THEN
            RAISE WARNING 'WP-567: blocked probable duplicate grant, external_id=% already recorded under a different event_id (this event_id=%)', v_external_id, p_event_id;
            RETURN 0;
        END;

        IF v_claimed_event IS NULL THEN
            RETURN 0;
        END IF;

        RETURN v_effective;
    END IF;

    v_level := public._lookup_qualification_level(p_account_id);
    v_stage := public._lookup_student_stage(p_account_id);

    IF v_level BETWEEN 1 AND 3 THEN
        v_level := v_level;
    ELSIF v_level = 4 THEN
        v_level := COALESCE(v_stage, 1);
    ELSIF v_level BETWEEN 5 AND 11 THEN
        v_level := v_level + 1;
    ELSE
        v_level := COALESCE(v_stage, 1);
    END IF;

    v_level := GREATEST(1, LEAST(12, v_level));

    SELECT qual_mult, action_cap
    INTO v_qual_mult, v_action_cap
    FROM _foreign_reference.qualification_levels_v4
    WHERE level_number = v_level;

    v_bonuses_eligible := COALESCE(v_stage, 0) >= 2;

    IF COALESCE(v_streak_eligible, FALSE) THEN
        v_streak_mult := COALESCE(public._compute_streak_mult(p_account_id, p_ingested_at), 1.0);
    END IF;

    IF v_effort_minutes IS NOT NULL AND v_effort_minutes > 0 THEN
        v_effort_factor := POWER(v_effort_minutes::NUMERIC, 0.6);
        v_base := v_effort_factor * COALESCE(v_rarity_mult, 1.0) * COALESCE(v_group_mult, 1);
    ELSIF v_is_marker THEN
        v_base := v_amount * COALESCE(v_rarity_mult, 1.0) * COALESCE(v_group_mult, 1);
    ELSE
        v_base := COALESCE(v_amount, 0) * COALESCE(v_rarity_mult, 1.0) * COALESCE(v_group_mult, 1);
    END IF;

    v_raw := v_base * v_qual_mult * v_streak_mult;
    v_effective := LEAST(v_raw, v_action_cap);
    v_cap_truncated := (v_effective < v_raw);

    v_daily_total_cap := v_action_cap * v_K;

    SELECT COALESCE(SUM(effective), 0) INTO v_today_total
    FROM public.applied_events
    WHERE account_id = p_account_id
      AND DATE(applied_at) = DATE(p_ingested_at);

    v_remaining_total := GREATEST(0, v_daily_total_cap - v_today_total);

    IF v_effective > v_remaining_total THEN
        v_effective := v_remaining_total;
        v_cap_truncated := TRUE;
    END IF;

    v_effective := ROUND(v_effective, 2);
    IF v_max_per_day_reached THEN
        v_effective := 0;
        v_cap_truncated := TRUE;
    END IF;

    -- WP-567: same unique_violation contract as the referral branch above --
    -- a semantic-key collision here is a real, distinct-event_id duplicate
    -- grant attempt; log and no-op rather than let it abort uncaught.
    BEGIN
        INSERT INTO public.applied_events (
            event_id, account_id, event_type, base_amount,
            dom_mult, qual_mult, streak_mult, daily_cap,
            raw_amount, effective, cap_truncated, bonuses_eligible,
            applied_at, payload_snapshot, rule_id, external_id
        ) VALUES (
            p_event_id, p_account_id, p_event_type, ROUND(v_base, 2),
            1.0, v_qual_mult, v_streak_mult, v_action_cap,
            ROUND(v_raw, 2), v_effective, v_cap_truncated, v_bonuses_eligible,
            p_ingested_at,
            jsonb_build_object(
                'level', v_level,
                'stage', v_stage,
                'bonuses_eligible', v_bonuses_eligible,
                'effort_minutes', v_effort_minutes,
                'group_mult', v_group_mult,
                'rarity_mult', v_rarity_mult,
                'max_per_day_reached', v_max_per_day_reached,
                'daily_total_cap', v_daily_total_cap
            ),
            v_rule_id, v_external_id
        )
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id INTO v_claimed_event;
    EXCEPTION WHEN unique_violation THEN
        RAISE WARNING 'WP-567: blocked probable duplicate grant, external_id=% already recorded under a different event_id (this event_id=%)', v_external_id, p_event_id;
        RETURN 0;
    END;

    IF v_claimed_event IS NULL THEN
        RETURN 0;
    END IF;

    IF v_bonuses_eligible THEN
        v_points_delta := v_effective;
    ELSE
        v_points_delta := 0;
        IF v_effective > 0 THEN
            INSERT INTO public.point_balances (
                account_id, points, earned_total, last_updated, last_event_id
            ) VALUES (
                p_account_id, 0, v_effective, p_ingested_at, p_event_id
            )
            ON CONFLICT (account_id) DO UPDATE
            SET earned_total = point_balances.earned_total + EXCLUDED.earned_total,
                last_updated = EXCLUDED.last_updated,
                last_event_id = EXCLUDED.last_event_id;
        END IF;
    END IF;

    RETURN v_points_delta;
END;
$function$;

DO $postflight$
DECLARE
    v_owner regrole;
    v_sentinel_account UUID := '00000000-0000-0000-0000-000000000047';
    v_sentinel_event BIGINT := -47047047;
    v_result NUMERIC;
    v_row public.applied_events%ROWTYPE;
BEGIN
    SELECT proowner::regrole INTO v_owner
    FROM pg_proc
    WHERE proname = 'compute_effective_amount_v4'
      AND pronamespace = 'public'::regnamespace;

    IF EXISTS (SELECT 1 FROM public.applied_events WHERE event_id = v_sentinel_event) THEN
        RAISE EXCEPTION '047 postflight: sentinel event_id already exists -- pick a different sentinel';
    END IF;

    -- Fail-safe path only: no matching row exists in the foreign
    -- domain_event table for this sentinel id, so the lookup inside the
    -- function must miss cleanly (or error and recover) and still credit
    -- points normally with external_id left NULL. The success path (a real
    -- domain_event match producing a non-NULL external_id) is a live prod
    -- check to run manually after this migration, on the first real new
    -- event -- inserting a synthetic row into the OTHER database's
    -- production events ledger as part of an automated migration postflight
    -- is out of scope for what a nullable-lookup regression test needs.
    EXECUTE format('SET LOCAL ROLE %I', v_owner);

    v_result := public.compute_effective_amount_v4(
        v_sentinel_account, v_sentinel_event, 'referral_attributed',
        '{}'::jsonb, clock_timestamp()
    );

    RESET ROLE;

    SELECT * INTO v_row FROM public.applied_events WHERE event_id = v_sentinel_event;

    IF v_row.event_id IS NULL THEN
        RAISE EXCEPTION '047 postflight: no applied_events row written for sentinel event';
    END IF;

    IF v_row.external_id IS NOT NULL THEN
        RAISE EXCEPTION '047 postflight: sentinel has no matching domain_event, expected external_id NULL, got %', v_row.external_id;
    END IF;

    IF v_result IS NULL THEN
        RAISE EXCEPTION '047 postflight: compute_effective_amount_v4 returned NULL, expected a numeric delta';
    END IF;

    -- Sentinel cleanup -- this DO block is not wrapped in a rollback-marker
    -- sub-transaction (unlike migration 042) because DDL earlier in this
    -- same outer transaction (CREATE OR REPLACE FUNCTION) already commits
    -- atomically with everything else on COMMIT; deleting the sentinel row
    -- here keeps the postflight self-cleaning without touching that DDL.
    DELETE FROM public.applied_events WHERE event_id = v_sentinel_event;
    DELETE FROM public.point_balances WHERE account_id = v_sentinel_account
        AND last_event_id = v_sentinel_event;
END
$postflight$;

COMMIT;
"""


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        already_applied = await conn.fetchval(
            "SELECT pg_get_functiondef('public.compute_effective_amount_v4"
            "(uuid, bigint, text, jsonb, timestamptz)'::regprocedure) LIKE $1",
            f"%{DUAL_WRITE_MARKER}%",
        )
        if already_applied:
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
        print(f"Migration 047: {'dual-write applied + live postflight passed' if applied else 'already applied'}")
        await pool.close()

    asyncio.run(run())
