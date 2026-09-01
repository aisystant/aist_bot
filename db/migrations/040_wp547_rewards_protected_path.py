"""
Migration 040: WP-547 rewards protected burn path — roles, schema, functions, grants.

Historical record of the production cutover applied 2026-09-01 in a peer session
with Codex (MC-sessions:2026-09/01/2026-09-01-07-wp547-prod-cutover/). The burn
contour only (points_redeemer role, redeemed_events columns/guard trigger,
apply_confirmed_burn_v1, compute_available_for_burn) — the grant/projection cutover
(rewards_points_engine_owner FDW wiring, MDPW earned-only credit) is intentionally
out of scope, see DS-my-strategy/inbox/WP-547/wp547-systemic-fix-design.sql Part 1/2.

Operator prerequisites this migration does NOT perform (unchanged from the design
candidate): setting points_redeemer's login password and updating REWARDS_URL is a
separate manual step (done via `railway variable set REWARDS_URL --stdin`, not here)
— this migration only prepares the database side.

Trigger-recreation ordering fix (found live, not present in the original 31.08
candidate): CREATE/REPLACE of trigger functions and (re)creation of the triggers
that reference them must happen BEFORE ALTER FUNCTION ... OWNER TO/REVOKE, because
the deploying role loses EXECUTE on a function the moment it stops owning it, and
CREATE TRIGGER requires EXECUTE on the target function at creation time (Postgres
CREATE TRIGGER docs). The 31.08 design did the REVOKE/OWNER transfer for
fn_log_redeemed_status_change before re-creating trg_redeemed_events_audit —
harmless in the disposable test DB (no pre-existing trigger there), but broke on
the live DB (pre-existing legacy audit trigger, "permission denied for function
public.fn_log_redeemed_status_change").

Manual run:
    REWARDS_URL=<dsn as neondb_owner/db-owner role> python -m db.migrations.040_wp547_rewards_protected_path
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BURN_PATH_SQL = """
BEGIN;

-- ============================================================
-- Part 0: least-privilege roles (credentials/user mappings are operator work)
-- ============================================================
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rewards_points_engine_owner') THEN
        CREATE ROLE rewards_points_engine_owner
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
            NOREPLICATION NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'projection_writer_rewards') THEN
        CREATE ROLE projection_writer_rewards
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
            NOREPLICATION NOBYPASSRLS;
    END IF;
END
$roles$;

-- Fail on unsafe pre-existing roles. A normal Neon deploy owner cannot ALTER
-- SUPERUSER/REPLICATION/BYPASSRLS attributes even to turn them off, so drift is
-- an operator repair, never something this migration silently normalizes.
DO $role_preflight$
DECLARE
    v_role RECORD;
BEGIN
    SELECT * INTO v_role FROM pg_roles
    WHERE rolname = 'rewards_points_engine_owner';
    IF v_role.rolcanlogin
       OR v_role.rolsuper
       OR v_role.rolcreatedb
       OR v_role.rolcreaterole
       OR v_role.rolinherit
       OR v_role.rolreplication
       OR v_role.rolbypassrls THEN
        RAISE EXCEPTION
            'WP-547 preflight: rewards_points_engine_owner has unsafe attributes';
    END IF;

    SELECT * INTO v_role FROM pg_roles
    WHERE rolname = 'projection_writer_rewards';
    IF NOT v_role.rolcanlogin
       OR v_role.rolsuper
       OR v_role.rolcreatedb
       OR v_role.rolcreaterole
       OR v_role.rolinherit
       OR v_role.rolreplication
       OR v_role.rolbypassrls THEN
        RAISE EXCEPTION
            'WP-547 preflight: projection_writer_rewards has unsafe attributes';
    END IF;

    IF pg_has_role(
        'projection_writer_rewards', 'rewards_points_engine_owner', 'MEMBER'
    ) THEN
        RAISE EXCEPTION
            'WP-547 preflight: projection_writer_rewards is a member of the engine owner role';
    END IF;
END
$role_preflight$;

-- PostgreSQL requires both SET ROLE ability and schema CREATE privilege before
-- ALTER FUNCTION ... OWNER. On PostgreSQL 17 a CREATEROLE-created role is granted
-- back to its creator with SET FALSE, so make the migration requirement explicit.
GRANT rewards_points_engine_owner TO CURRENT_USER
    WITH SET TRUE, INHERIT FALSE;
GRANT CREATE ON SCHEMA public TO rewards_points_engine_owner;

DO $writer_schema_preflight$
BEGIN
    IF has_schema_privilege(
        'projection_writer_rewards', 'public', 'CREATE'
    ) THEN
        RAISE EXCEPTION
            'WP-547 preflight: projection_writer_rewards can CREATE in public; revoke direct/PUBLIC CREATE first';
    END IF;
END
$writer_schema_preflight$;

BEGIN;

DO $burn_role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'points_redeemer') THEN
        CREATE ROLE points_redeemer
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
            NOREPLICATION NOBYPASSRLS;
    END IF;
END
$burn_role$;

DO $burn_role_preflight$
DECLARE
    v_role RECORD;
BEGIN
    SELECT * INTO v_role FROM pg_roles WHERE rolname = 'points_redeemer';
    IF NOT v_role.rolcanlogin
       OR v_role.rolsuper
       OR v_role.rolcreatedb
       OR v_role.rolcreaterole
       OR v_role.rolinherit
       OR v_role.rolreplication
       OR v_role.rolbypassrls THEN
        RAISE EXCEPTION 'WP-547 burn preflight: points_redeemer has unsafe attributes';
    END IF;

    IF pg_has_role('points_redeemer', 'rewards_points_engine_owner', 'MEMBER')
       OR pg_has_role('points_redeemer', 'projection_writer_rewards', 'MEMBER') THEN
        RAISE EXCEPTION 'WP-547 burn preflight: points_redeemer inherits a writer/owner role';
    END IF;
END
$burn_role_preflight$;

ALTER TABLE public.redeemed_events
    ADD COLUMN IF NOT EXISTS balance_apply_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS balance_applied_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS balance_apply_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS balance_apply_last_error TEXT,
    ADD COLUMN IF NOT EXISTS balance_apply_failed_at TIMESTAMPTZ;

DO $burn_constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.redeemed_events'::REGCLASS
          AND conname = 'redeemed_events_balance_apply_state_check'
    ) THEN
        ALTER TABLE public.redeemed_events
            ADD CONSTRAINT redeemed_events_balance_apply_state_check CHECK (
                balance_apply_attempts >= 0
                AND (
                    NOT balance_apply_eligible
                    OR status = 'confirmed'
                )
                AND (
                    balance_applied_at IS NULL
                    OR (
                        balance_apply_eligible
                        AND status = 'confirmed'
                        AND balance_apply_failed_at IS NULL
                    )
                )
                AND (
                    balance_apply_failed_at IS NULL
                    OR (
                        balance_apply_eligible
                        AND status = 'confirmed'
                        AND balance_applied_at IS NULL
                        AND balance_apply_last_error IS NOT NULL
                    )
                )
            );
    END IF;
END
$burn_constraints$;

CREATE INDEX IF NOT EXISTS idx_redeemed_events_balance_apply_pending
    ON public.redeemed_events (confirmed_at, payment_id)
    WHERE status = 'confirmed'
      AND balance_apply_eligible
      AND balance_applied_at IS NULL
      AND balance_apply_failed_at IS NULL;

COMMENT ON COLUMN public.redeemed_events.balance_apply_eligible IS
    'WP-547 cutover fence. FALSE for every legacy row; only bridge/final bot versions opt a confirmed row into MDPW burn apply.';
COMMENT ON COLUMN public.redeemed_events.balance_applied_at IS
    'Target-local idempotency marker committed atomically with the point_balances debit.';
COMMENT ON COLUMN public.redeemed_events.balance_apply_failed_at IS
    'Terminal poison marker. Scanner excludes the row until an explicit audited manual requeue.';

-- The application role owns the ledger lifecycle but can neither forge an
-- applied marker nor write point_balances directly. During bridge A it invokes
-- only the protected function below; bot B loses even that EXECUTE privilege.
REVOKE ALL PRIVILEGES ON public.redeemed_events
    FROM points_redeemer CASCADE;
DO $sanitize_redeemer_column_acl$
DECLARE
    v_column NAME;
BEGIN
    FOR v_column IN
        SELECT attribute_entry.attname
        FROM pg_attribute attribute_entry
        CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_entry.attacl) acl_entry
        WHERE attribute_entry.attrelid = 'public.redeemed_events'::REGCLASS
          AND acl_entry.grantee = 'points_redeemer'::REGROLE
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES (%I) ON public.redeemed_events '
            'FROM points_redeemer CASCADE',
            v_column
        );
    END LOOP;
END
$sanitize_redeemer_column_acl$;

DO $grant_redeemer_connect$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO points_redeemer',
        current_database()
    );
END
$grant_redeemer_connect$;
GRANT USAGE ON SCHEMA public, _foreign_reference, _foreign_indicators
    TO points_redeemer;
GRANT SELECT ON
    public.redeemed_events,
    public.point_balances,
    public.applied_events,
    public.typing_daily,
    _foreign_reference.qualification_levels_v4,
    _foreign_reference.loyalty_pool_config,
    _foreign_reference.qualification_multipliers,
    _foreign_indicators.calculated_profile
    TO points_redeemer;
GRANT INSERT (
    payment_id, account_id, points_amount, discount_rub,
    qualification_snapshot, daily_cap_snapshot, payment_source, purpose,
    rollback_reason, reserved_at, rolled_back_at,
    metadata, expires_at, product_code
) ON public.redeemed_events TO points_redeemer;
GRANT UPDATE (
    payment_id, status, rollback_reason, confirmed_at, rolled_back_at,
    expires_at, balance_apply_eligible
) ON public.redeemed_events TO points_redeemer;

GRANT SELECT, UPDATE ON public.redeemed_events
    TO rewards_points_engine_owner;
REVOKE INSERT ON public.redeemed_events_audit
    FROM points_redeemer CASCADE;
REVOKE USAGE, SELECT ON SEQUENCE public.redeemed_events_audit_audit_id_seq
    FROM points_redeemer CASCADE;
GRANT INSERT ON public.redeemed_events_audit
    TO rewards_points_engine_owner;
GRANT USAGE, SELECT ON SEQUENCE public.redeemed_events_audit_audit_id_seq
    TO rewards_points_engine_owner;
GRANT SELECT ON public.redeemed_events TO projection_writer_rewards;

-- The historical fence is a database invariant, not a Python convention.
-- Old/owner bot traffic after schema install may still confirm with eligible=false;
-- the dedicated bridge role must always perform reserved→confirmed + opt-in in
-- one statement and can never opt an already-confirmed legacy row in later.

CREATE OR REPLACE FUNCTION public.fn_guard_redeemed_apply_state()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.balance_apply_eligible THEN
        RAISE EXCEPTION 'WP-547 burn guard: INSERT cannot opt into balance apply';
    END IF;

    IF TG_OP = 'UPDATE' AND current_user = 'points_redeemer' THEN
        IF OLD.status IS DISTINCT FROM NEW.status
           AND NOT (
               OLD.status = 'reserved'
               AND NEW.status IN ('confirmed', 'rolled_back')
           ) THEN
            RAISE EXCEPTION
                'WP-547 burn guard: invalid bot ledger transition % -> %',
                OLD.status, NEW.status;
        END IF;

        IF OLD.payment_id IS DISTINCT FROM NEW.payment_id
           AND NOT (OLD.status = 'reserved' AND NEW.status = 'reserved') THEN
            RAISE EXCEPTION
                'WP-547 burn guard: payment_id is immutable after reservation';
        END IF;

        IF OLD.status = 'reserved' AND NEW.status = 'confirmed'
           AND NOT NEW.balance_apply_eligible THEN
            RAISE EXCEPTION
                'WP-547 burn guard: bridge confirm must opt into balance apply';
        END IF;

        IF NOT OLD.balance_apply_eligible AND NEW.balance_apply_eligible
           AND NOT (OLD.status = 'reserved' AND NEW.status = 'confirmed') THEN
            RAISE EXCEPTION
                'WP-547 burn guard: only reserved-to-confirmed may opt in';
        END IF;

        IF OLD.balance_apply_eligible AND NOT NEW.balance_apply_eligible THEN
            RAISE EXCEPTION
                'WP-547 burn guard: bridge role cannot clear apply eligibility';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_redeemed_events_apply_guard
    ON public.redeemed_events;
CREATE TRIGGER trg_redeemed_events_apply_guard
    BEFORE INSERT OR UPDATE OF payment_id, status, balance_apply_eligible
    ON public.redeemed_events
    FOR EACH ROW
    EXECUTE FUNCTION public.fn_guard_redeemed_apply_state();

-- Audit marker/failure transitions as well as status transitions. This creates
-- per-payment ground truth for every new bridge row without pretending that it
-- reconstructs historical direct debits.

CREATE OR REPLACE FUNCTION public.fn_log_redeemed_status_change()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.redeemed_events_audit (
            payment_id, old_status, new_status, metadata
        ) VALUES (
            NEW.payment_id,
            NULL,
            NEW.status,
            jsonb_build_object(
                'balance_apply_eligible', NEW.balance_apply_eligible,
                'balance_applied_at', NEW.balance_applied_at,
                'balance_apply_failed_at', NEW.balance_apply_failed_at
            )
        );
    ELSIF OLD.status IS DISTINCT FROM NEW.status
       OR OLD.metadata IS DISTINCT FROM NEW.metadata
       OR OLD.rollback_reason IS DISTINCT FROM NEW.rollback_reason
       OR OLD.balance_apply_eligible IS DISTINCT FROM NEW.balance_apply_eligible
       OR OLD.balance_applied_at IS DISTINCT FROM NEW.balance_applied_at
       OR OLD.balance_apply_attempts IS DISTINCT FROM NEW.balance_apply_attempts
       OR OLD.balance_apply_last_error IS DISTINCT FROM NEW.balance_apply_last_error
       OR OLD.balance_apply_failed_at IS DISTINCT FROM NEW.balance_apply_failed_at THEN
        INSERT INTO public.redeemed_events_audit (
            payment_id, old_status, new_status, metadata
        ) VALUES (
            NEW.payment_id,
            OLD.status,
            NEW.status,
            jsonb_build_object(
                'rollback_reason', NEW.rollback_reason,
                'metadata_changed', OLD.metadata IS DISTINCT FROM NEW.metadata,
                'balance_apply_eligible', NEW.balance_apply_eligible,
                'balance_applied_at', NEW.balance_applied_at,
                'balance_apply_attempts', NEW.balance_apply_attempts,
                'balance_apply_last_error', NEW.balance_apply_last_error,
                'balance_apply_failed_at', NEW.balance_apply_failed_at
            )
        );
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_redeemed_events_audit ON public.redeemed_events;
CREATE TRIGGER trg_redeemed_events_audit
    AFTER INSERT OR UPDATE OF
        status, metadata, rollback_reason, balance_apply_eligible,
        balance_applied_at, balance_apply_attempts,
        balance_apply_last_error, balance_apply_failed_at
    ON public.redeemed_events
    FOR EACH ROW
    EXECUTE FUNCTION public.fn_log_redeemed_status_change();

REVOKE ALL ON FUNCTION public.fn_log_redeemed_status_change()
    FROM PUBLIC, points_redeemer, projection_writer_rewards CASCADE;
ALTER FUNCTION public.fn_log_redeemed_status_change()
    OWNER TO rewards_points_engine_owner;

CREATE OR REPLACE FUNCTION public.apply_confirmed_burn_v1(
    p_payment_id TEXT
) RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
SET timezone = 'UTC'
AS $function$
DECLARE
    v_account_id UUID;
    v_points_amount NUMERIC(10, 2);
    v_current_points NUMERIC;
    v_eligible BOOLEAN;
    v_applied_at TIMESTAMPTZ;
    v_failed_at TIMESTAMPTZ;
    v_last_error TEXT;
BEGIN
    IF p_payment_id IS NULL OR p_payment_id = '' THEN
        RAISE EXCEPTION 'WP-547 burn: payment_id is required'
            USING ERRCODE = '22004';
    END IF;

    -- Read only the lock key first. Revalidate every state under the account
    -- lock and then the ledger row lock; reserve/grant use the same namespace.
    SELECT account_id INTO v_account_id
    FROM public.redeemed_events
    WHERE payment_id = p_payment_id;
    IF NOT FOUND THEN
        RETURN 'not_ready';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('wp547:account:' || v_account_id::TEXT, 0)
    );

    SELECT points_amount, balance_apply_eligible,
           balance_applied_at, balance_apply_failed_at,
           balance_apply_last_error
    INTO v_points_amount, v_eligible, v_applied_at, v_failed_at,
         v_last_error
    FROM public.redeemed_events
    WHERE payment_id = p_payment_id
      AND status = 'confirmed'
    FOR UPDATE;

    IF NOT FOUND OR NOT v_eligible THEN
        RETURN 'not_ready';
    END IF;
    IF v_applied_at IS NOT NULL THEN
        RETURN 'already_applied';
    END IF;
    IF v_failed_at IS NOT NULL THEN
        RETURN CASE v_last_error
            WHEN 'insufficient_balance' THEN 'manual_review_insufficient_balance'
            WHEN 'balance_missing' THEN 'manual_review_balance_missing'
            ELSE 'manual_review'
        END;
    END IF;

    SELECT points INTO v_current_points
    FROM public.point_balances
    WHERE account_id = v_account_id
    FOR UPDATE;

    IF NOT FOUND THEN
        UPDATE public.redeemed_events
        SET balance_apply_attempts = balance_apply_attempts + 1,
            balance_apply_last_error = 'balance_missing',
            balance_apply_failed_at = clock_timestamp()
        WHERE payment_id = p_payment_id;
        RETURN 'manual_review_balance_missing';
    END IF;

    IF v_current_points < v_points_amount THEN
        UPDATE public.redeemed_events
        SET balance_apply_attempts = balance_apply_attempts + 1,
            balance_apply_last_error = 'insufficient_balance',
            balance_apply_failed_at = clock_timestamp()
        WHERE payment_id = p_payment_id;
        RETURN 'manual_review_insufficient_balance';
    END IF;

    UPDATE public.point_balances
    SET points = points - v_points_amount,
        last_updated = clock_timestamp()
    WHERE account_id = v_account_id;

    UPDATE public.redeemed_events
    SET balance_applied_at = clock_timestamp(),
        balance_apply_attempts = balance_apply_attempts + 1,
        balance_apply_last_error = NULL,
        balance_apply_failed_at = NULL
    WHERE payment_id = p_payment_id;

    RETURN 'applied';
END;
$function$;

REVOKE ALL ON FUNCTION public.apply_confirmed_burn_v1(TEXT)
    FROM PUBLIC, points_redeemer, projection_writer_rewards CASCADE;
GRANT EXECUTE ON FUNCTION public.apply_confirmed_burn_v1(TEXT)
    TO rewards_points_engine_owner, projection_writer_rewards, points_redeemer;
ALTER FUNCTION public.apply_confirmed_burn_v1(TEXT)
    OWNER TO rewards_points_engine_owner;

-- Display and reserve use the same conservative pending-burn formula. The
-- account advisory lock is acquired by reserve_burn before its final check and
-- INSERT; this helper alone is a read, not a synchronization primitive.

CREATE OR REPLACE FUNCTION public.compute_available_for_burn(
    p_account_id UUID,
    p_today_start TIMESTAMPTZ DEFAULT date_trunc('day', now())
) RETURNS NUMERIC(10, 2)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public, pg_temp
SET timezone = 'UTC'
AS $function$
    SELECT GREATEST(
        0,
        COALESCE((
            SELECT points
            FROM public.point_balances
            WHERE account_id = p_account_id
        ), 0)
        - COALESCE((
            SELECT SUM(points_amount)
            FROM public.redeemed_events
            WHERE account_id = p_account_id
              AND (
                  status = 'reserved'
                  OR (
                      status = 'confirmed'
                      AND balance_apply_eligible
                      AND balance_applied_at IS NULL
                  )
              )
        ), 0)
    )::NUMERIC(10, 2);
$function$;

REVOKE ALL ON FUNCTION public.compute_available_for_burn(UUID, TIMESTAMPTZ)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.compute_available_for_burn(UUID, TIMESTAMPTZ)
    TO points_redeemer, projection_writer_rewards, rewards_points_engine_owner;

DO $burn_privilege_postflight$
BEGIN
    IF has_table_privilege(
        'points_redeemer', 'public.point_balances', 'INSERT'
    ) OR has_table_privilege(
        'points_redeemer', 'public.point_balances', 'UPDATE'
    ) OR has_any_column_privilege(
        'points_redeemer', 'public.point_balances', 'INSERT'
    ) OR has_any_column_privilege(
        'points_redeemer', 'public.point_balances', 'UPDATE'
    ) THEN
        RAISE EXCEPTION 'WP-547 burn postflight: bot can write point_balances directly';
    END IF;

    IF has_column_privilege(
        'points_redeemer', 'public.redeemed_events', 'balance_applied_at', 'UPDATE'
    ) OR has_column_privilege(
        'points_redeemer', 'public.redeemed_events', 'balance_apply_attempts', 'UPDATE'
    ) OR has_column_privilege(
        'points_redeemer', 'public.redeemed_events', 'balance_apply_last_error', 'UPDATE'
    ) OR has_column_privilege(
        'points_redeemer', 'public.redeemed_events', 'balance_apply_failed_at', 'UPDATE'
    ) THEN
        RAISE EXCEPTION 'WP-547 burn postflight: bot can forge burn apply state';
    END IF;

    IF has_table_privilege(
        'points_redeemer', 'public.redeemed_events_audit', 'INSERT'
    ) OR has_sequence_privilege(
        'points_redeemer',
        'public.redeemed_events_audit_audit_id_seq',
        'USAGE'
    ) THEN
        RAISE EXCEPTION 'WP-547 burn postflight: bot can forge the burn audit';
    END IF;

    IF has_column_privilege(
        'points_redeemer', 'public.redeemed_events', 'status', 'INSERT'
    ) OR has_column_privilege(
        'points_redeemer', 'public.redeemed_events', 'balance_apply_eligible', 'INSERT'
    ) THEN
        RAISE EXCEPTION 'WP-547 burn postflight: bot can insert a pre-confirmed/eligible row';
    END IF;

    IF NOT has_function_privilege(
        'projection_writer_rewards',
        'public.apply_confirmed_burn_v1(text)',
        'EXECUTE'
    ) OR NOT has_function_privilege(
        'points_redeemer',
        'public.apply_confirmed_burn_v1(text)',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'WP-547 burn postflight: bridge executor privilege missing';
    END IF;
END
$burn_privilege_postflight$;

COMMIT;
"""

SUPPLEMENTARY_GRANTS_SQL = """
BEGIN;

GRANT INSERT (chat_id, from_tier, to_tier, reason)
    ON public.tier_events TO points_redeemer;
GRANT USAGE ON SEQUENCE public.tier_events_id_seq TO points_redeemer;

GRANT INSERT (account_id, snapshot_date)
    ON public.subscribers_snapshot TO points_redeemer;
GRANT SELECT (account_id, snapshot_date)
    ON public.subscribers_snapshot TO points_redeemer;
GRANT DELETE
    ON public.subscribers_snapshot TO points_redeemer;

GRANT SELECT (account_id)
    ON public.first_payment_guard TO points_redeemer;

COMMIT;
"""


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'points_redeemer')"
        )
        if exists:
            return False

    async with pool.acquire() as conn:
        await conn.execute(BURN_PATH_SQL)
        await conn.execute(SUPPLEMENTARY_GRANTS_SQL)
    return True


if __name__ == "__main__":
    dsn = os.environ.get("REWARDS_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("Error: REWARDS_URL not set", file=sys.stderr)
        sys.exit(1)

    async def run():
        pool = await asyncpg.create_pool(dsn)
        created = await migrate_if_needed(pool)
        print(f"Migration 040: {'burn protected path applied' if created else 'already applied (points_redeemer exists)'}")
        await pool.close()

    asyncio.run(run())
