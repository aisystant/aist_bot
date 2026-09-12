"""
Migration 048: WP-567 Ф1 -- transfer ownership of compute_effective_amount_v4
from neondb_owner to the narrow rewards_points_engine_owner role.

Context (peer-session 2026-09-12-07-wp567-zavershenie-faz, Claude+Kimi+Codex,
same day as migration 047 and WP-567's precondition B): the grant/accrual
path is the last piece of WP-567 still running under the wide neondb_owner
account. The burn/redeem path was already moved to rewards_points_engine_owner
in WP-547. This migration is the equivalent cutover for the grant side.

MUST run only after the companion script
`2026-09-12-f1-precondition-fdw-setup.py` (artifact A, WP-567 archive) has
been applied: that script gives rewards_points_engine_owner working FDW
access to _foreign_learning.domain_event and _foreign_indicators.calculated_profile
across three separate databases -- access this migration's own preflight
checks for and refuses to proceed without, but does not itself grant,
because PostgreSQL has no cross-database transactions and this file's
mutations must stay inside one all-or-nothing transaction on `rewards`.

Two-and-a-half round review (Codex as db-safety-reviewer) found and fixed,
before this file existed:
  - The obvious "revoke excess grants, then ALTER OWNER" one-liner missed
    that compute_effective_amount_v4 calls two SECURITY INVOKER helpers
    (_lookup_qualification_level, _lookup_student_stage) that read
    _foreign_indicators.calculated_profile -- a SECURITY INVOKER function
    called from inside a SECURITY DEFINER one runs as the DEFINER's
    effective owner, so once ownership moves, those two calls would start
    failing with permission denied for every code path except the early-
    return referral_attributed branch. Fixed by treating
    _foreign_indicators access as required, not excess (see artifact A).
  - "REVOKE excess grants" was correctly kept -- 6 _foreign_reference tables
    (activity_domain_multipliers, event_type_domain_map, qualification_level,
    qualification_multipliers, repo_domain_map, student_stage_multipliers)
    that compute_effective_amount_v4 and its helpers never touch, inherited
    from an earlier, wider WP-547 design-file grant.
  - Preflight (checked BEFORE any mutation, not folded into the postflight
    like migration 047's lookup-only checks) must confirm: exact function
    signature (not bare proname, to rule out overload ambiguity); the
    migrating role can actually SET ROLE into rewards_points_engine_owner
    and that role has CREATE on the public schema (ALTER ... OWNER TO
    otherwise fails, or on older PostgreSQL semantics could silently not
    behave as expected); has_table_privilege/has_schema_privilege on all
    five target objects (three _foreign_reference tables + the two above) --
    not information_schema.role_table_grants, which does not see access
    granted through PUBLIC or role membership (exactly the class of gap the
    precondition-B independent review found earlier the same day).
  - Postflight must exercise BOTH branches with separate sentinel accounts:
    referral_attributed (early return, never touches the helpers) and the
    general branch (calls both helpers). A single sentinel through only the
    early-return branch would pass even if the helper grants were entirely
    broken.
  - "Rollback is one ALTER OWNER back" is only half true and is not treated
    as a substitute for testing here: it restores who owns the function
    instantly, but does not undo the REVOKE of the six excess grants or the
    new grants added for the narrow role. Both are cheap, additive/
    subtractive ACL changes with no data impact, so this migration does not
    attempt a scripted combined rollback -- reverting ownership and
    separately deciding whether to restore the six-table grant are treated
    as two independent, cheap decisions if ever needed, not one operation.

Deliberately out of scope (flagged for the pilot, not decided here):
neondb_owner keeps its existing (pre-existing, unrelated to this migration)
access to _foreign_learning and _foreign_indicators after the cutover --
whether to narrow that too is a separate decision outside this migration's
blast radius.

NOT applied to production by the agent that wrote this file -- prepared for
review only, same as migration 047 earlier today. Whoever runs this needs
REWARDS_URL with rights to ALTER the function (must already own it or be
superuser) and to REVOKE/GRANT on rewards_points_engine_owner.

Precondition: run `2026-09-12-f1-precondition-fdw-setup.py`
(archive/wp-contexts/WP-567/ in DS-my-strategy) first -- this migration's
own preflight refuses to proceed if that has not been done.

Manual run:
    REWARDS_URL=<dsn as neondb_owner/db-owner role> python -m db.migrations.048_wp567_grant_cutover
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

CUTOVER_MARKER = "wp567_f1_grant_cutover"

MIGRATION_SQL = """
BEGIN;

DO $preflight$
DECLARE
    v_current_owner regrole;
    v_target_role CONSTANT text := 'rewards_points_engine_owner';
BEGIN
    -- Exact signature, not bare proname: rules out an overload match error.
    SELECT proowner INTO v_current_owner
    FROM pg_proc
    WHERE proname = 'compute_effective_amount_v4'
      AND pronamespace = 'public'::regnamespace
      AND pg_get_function_identity_arguments(oid) =
          'p_account_id uuid, p_event_id bigint, p_event_type text, p_payload jsonb, p_ingested_at timestamp with time zone';

    IF v_current_owner IS NULL THEN
        RAISE EXCEPTION '048 preflight: compute_effective_amount_v4 with the expected signature not found';
    END IF;

    IF NOT (current_user = v_current_owner::text OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)) THEN
        RAISE EXCEPTION '048 preflight: % is neither the current owner (%) nor superuser -- cannot ALTER OWNER', current_user, v_current_owner;
    END IF;

    IF NOT pg_has_role(current_user, v_target_role, 'SET') THEN
        RAISE EXCEPTION '048 preflight: % cannot SET ROLE %, required for the ownership transfer', current_user, v_target_role;
    END IF;

    IF NOT has_schema_privilege(v_target_role, 'public', 'CREATE') THEN
        RAISE EXCEPTION '048 preflight: % lacks CREATE on schema public, ALTER FUNCTION OWNER TO would leave it unable to own the function correctly', v_target_role;
    END IF;

    -- Artifact A (precondition, run separately -- see file docstring) must
    -- already have granted this. has_table_privilege/has_schema_privilege,
    -- not information_schema.role_table_grants: that view misses PUBLIC and
    -- role-membership grants (the class of gap the same-day precondition-B
    -- independent review found).
    IF NOT (
        has_table_privilege(v_target_role, '_foreign_reference.reward_rules', 'SELECT')
        AND has_table_privilege(v_target_role, '_foreign_reference.loyalty_pool_config', 'SELECT')
        AND has_table_privilege(v_target_role, '_foreign_reference.qualification_levels_v4', 'SELECT')
        AND has_table_privilege(v_target_role, '_foreign_learning.domain_event', 'SELECT')
        AND has_table_privilege(v_target_role, '_foreign_indicators.calculated_profile', 'SELECT')
    ) THEN
        RAISE EXCEPTION '048 preflight: % is missing required SELECT on one or more foreign tables -- run the artifact-A precondition script first', v_target_role;
    END IF;

    IF NOT (
        has_function_privilege(v_target_role, 'public._lookup_qualification_level(uuid)', 'EXECUTE')
        AND has_function_privilege(v_target_role, 'public._lookup_student_stage(uuid)', 'EXECUTE')
    ) THEN
        RAISE EXCEPTION '048 preflight: % lacks EXECUTE on the SECURITY INVOKER helpers compute_effective_amount_v4 depends on', v_target_role;
    END IF;
END
$preflight$;

-- Excess grants inherited from an earlier, wider WP-547 design-file grant
-- (`GRANT SELECT ON ALL TABLES IN SCHEMA _foreign_reference, _foreign_indicators`,
-- applied 2026-09-02) -- compute_effective_amount_v4 and its helpers never
-- touch these six tables. _foreign_indicators.calculated_profile is a
-- separate, genuinely-needed grant and is intentionally NOT in this list.
REVOKE SELECT ON
    _foreign_reference.activity_domain_multipliers,
    _foreign_reference.event_type_domain_map,
    _foreign_reference.qualification_level,
    _foreign_reference.qualification_multipliers,
    _foreign_reference.repo_domain_map,
    _foreign_reference.student_stage_multipliers
FROM rewards_points_engine_owner;

ALTER FUNCTION public.compute_effective_amount_v4(
    uuid, bigint, text, jsonb, timestamp with time zone
) OWNER TO rewards_points_engine_owner;

DO $postflight$
DECLARE
    v_sentinel_account_referral UUID := '00000000-0000-0000-0000-000000000048';
    v_sentinel_event_referral BIGINT := -48048001;
    v_sentinel_account_general UUID := '00000000-0000-0000-0000-000000000049';
    v_sentinel_event_general BIGINT := -48048002;
    v_new_owner regrole;
    v_result_referral NUMERIC;
    v_result_general NUMERIC;
BEGIN
    -- Same exact-signature identity as the preflight DO-block above
    -- (cold review, 2026-09-12): a bare proname+namespace match here would
    -- silently pick an arbitrary overload if one is ever added later,
    -- exactly the ambiguity the preflight above was written to rule out.
    SELECT proowner INTO v_new_owner
    FROM pg_proc
    WHERE oid = 'public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)'::regprocedure;

    IF v_new_owner::text != 'rewards_points_engine_owner' THEN
        RAISE EXCEPTION '048 postflight: owner is % after ALTER, expected rewards_points_engine_owner', v_new_owner;
    END IF;

    IF EXISTS (SELECT 1 FROM public.applied_events WHERE event_id IN (v_sentinel_event_referral, v_sentinel_event_general)) THEN
        RAISE EXCEPTION '048 postflight: sentinel event_id already exists -- pick different sentinels';
    END IF;

    -- Branch 1: referral_attributed -- the early-return path. Confirms the
    -- ownership transfer itself does not break the simple case.
    v_result_referral := public.compute_effective_amount_v4(
        v_sentinel_account_referral, v_sentinel_event_referral, 'referral_attributed',
        '{}'::jsonb, clock_timestamp()
    );
    IF v_result_referral IS NULL THEN
        RAISE EXCEPTION '048 postflight: referral_attributed branch returned NULL';
    END IF;

    -- Branch 2: a real, always-on marker event ('ai_chat' -- reward_kind
    -- 'points', match_condition NULL, valid_to NULL as of 2026-09-12) --
    -- this is the branch that actually calls
    -- _lookup_qualification_level/_lookup_student_stage. An unmatched
    -- event_type would short-circuit on "IF v_rule_id IS NULL THEN RETURN 0"
    -- before ever reaching the helpers, silently passing this check without
    -- testing anything -- the sentinel event_type must resolve to a real
    -- rule. Migration 047's own postflight only exercised branch 1; a cold
    -- review earlier today flagged that as insufficient for verifying THIS
    -- migration specifically, since branch 1 never touches the helpers this
    -- cutover is really testing.
    v_result_general := public.compute_effective_amount_v4(
        v_sentinel_account_general, v_sentinel_event_general, 'ai_chat',
        '{}'::jsonb, clock_timestamp()
    );
    IF v_result_general IS NULL THEN
        RAISE EXCEPTION '048 postflight: general branch (ai_chat) returned NULL, expected a numeric delta';
    END IF;

    DELETE FROM public.applied_events WHERE event_id IN (v_sentinel_event_referral, v_sentinel_event_general);
    DELETE FROM public.point_balances WHERE last_event_id IN (v_sentinel_event_referral, v_sentinel_event_general);
END
$postflight$;

COMMIT;
"""


FUNCTION_IDENTITY = "public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)"


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        # Exact-signature regprocedure lookup, same identity used by the
        # preflight/postflight DO-blocks above -- a bare proname+namespace
        # match would silently pick an arbitrary overload if one is ever
        # added (cold review, 2026-09-12).
        owner_already_moved = await conn.fetchval(
            f"SELECT proowner::regrole::text = 'rewards_points_engine_owner' "
            f"FROM pg_proc WHERE oid = '{FUNCTION_IDENTITY}'::regprocedure"
        )
        if owner_already_moved:
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
        print(f"Migration 048: {'ownership cutover applied + live postflight passed' if applied else 'already applied'}")
        await pool.close()

    asyncio.run(run())
