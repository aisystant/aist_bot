"""WP-547 protected rewards path — permanent integration test suite.

Ported from the disposable peer-session test stand
(`/private/tmp/wp547-burn-pg16-{tests.sql,setup.sql,concurrency.py}`, sessions
2026-08-31/2026-09-01) into a permanent pytest file, per consensus in
MC-sessions/2026-09/01/2026-09-01-13-wp547-teststand-gdpr-docdrift/01-peer.md
"План сессии" п.4 and 02-writer.md.

**B11 отсутствует в исходном стенде и не реконструирован без спецификации.**
grep across all three original stand files confirms only B1-B10, B12, B13 ever
existed; there is no evidence B11 was ever a real test. Per consensus (01-peer.md
п.1) it is not invented here — the numbering gap below is intentional, not a bug
in this file.

Scope: burn path (migration 040, `apply_confirmed_burn_v1`) B1-B10/B12/B13, plus
new GDPR erase-path tests (migration 041, `erase_account_balance`) added this
session (peer-session plan п.3).

## Opt-in / DSN contract

Requires `WP547_PG_DSN` env var — a disposable/dedicated test Postgres, NOT a
shared or production database. The connecting role must be a superuser (or hold
equivalent CREATEROLE + admin-on-created-roles privileges): migration 040 issues
`CREATE ROLE`, `GRANT ... TO CURRENT_USER WITH SET TRUE`, and `ALTER FUNCTION ...
OWNER TO`, and the B13/erase-ACL tests use `SET ROLE points_redeemer` on the same
connection. Unset `WP547_PG_DSN` → the whole module is skipped, not failed.

## Fixture design — why not a full 001→040 migration replay

No migration runner exists in this repo (verified: no `run_migrations`/
`migrate_all` script, only bot.py's ad-hoc `importlib.import_module(...)` calls
for a handful of individually-gated legacy migrations). Per consensus
(01-peer.md, Codex ход 1, п.2) building a second deploy pipeline just for this
test is not worth it. Instead: a MINIMAL hand-written pre-schema (schemas/tables
migrations 040/041 actually touch) + the REAL `migrate_if_needed()` from both
migration modules, imported via `importlib` (numeric module names are not valid
Python identifiers — same pattern `bot.py` already uses for its own migration
imports).

**The one condition the pre-schema is NOT allowed to skip:** a pre-existing
LEGACY `trg_redeemed_events_audit` trigger + plain (non-SECURITY DEFINER,
no search_path pin) trigger function, created and owned by the deploying role
BEFORE migration 040 runs. This is not decorative — it is the exact condition
that let a live-only bug slip past the original disposable session-11 test DB
(no pre-existing trigger there) and reach production on 2026-09-01: migration
040's `ALTER FUNCTION ... OWNER TO` used to run before the trigger recreation,
so the deploying role lost `EXECUTE` on the function before `CREATE TRIGGER`
needed it. Migration 040 as shipped already fixes the ordering (CREATE/REPLACE
FUNCTION + trigger recreation before REVOKE/OWNER transfer) — this fixture
exists to keep that fix honest against regression, not to reproduce the bug.

## Per-test isolation, not one shared transaction

The original `.sql` stand ran everything inside one transaction ending in
`ROLLBACK`. That does not work for the concurrency tests (B4-B6): independent
`asyncpg` connections cannot see another transaction's uncommitted rows.
Per consensus (01-peer.md p.2) B4-B6 use committed setup + explicit cleanup by
each test's own unique account/payment namespace. This file generalizes that
pattern to every test for consistency: each test mints its own `uuid.uuid4()`
account id(s) and unique payment ids, commits its own rows, and cleans them up
in a `finally` block — so tests are independent of run order and of each other.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DSN_ENV = "WP547_PG_DSN"
_DSN = os.environ.get(DSN_ENV)

pytestmark = pytest.mark.skipif(
    not _DSN,
    reason=f"{DSN_ENV} not set — opt-in WP-547 integration test, skipped by default",
)

_ADMIN_QUAL = "ученик"
_ADMIN_CAP = 100

# ============================================================
# Minimal pre-schema: only what migrations 040/041 touch, plus a plain
# pre-existing (legacy) trg_redeemed_events_audit — see module docstring.
# ============================================================

_PRESCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS _foreign_reference;
CREATE SCHEMA IF NOT EXISTS _foreign_indicators;

CREATE TABLE _foreign_reference.loyalty_pool_config (
    use_v4_formula BOOLEAN NOT NULL DEFAULT TRUE,
    k INTEGER NOT NULL DEFAULT 10,
    valid_to TIMESTAMPTZ
);
CREATE TABLE _foreign_reference.qualification_levels_v4 (
    level_number INTEGER PRIMARY KEY,
    qual_mult NUMERIC NOT NULL,
    action_cap NUMERIC NOT NULL
);
CREATE TABLE _foreign_reference.qualification_multipliers (
    sort_order INTEGER PRIMARY KEY,
    qualification TEXT NOT NULL,
    daily_cap NUMERIC NOT NULL
);
INSERT INTO _foreign_reference.qualification_multipliers
    VALUES (1, 'ученик', 100);

CREATE TABLE _foreign_indicators.calculated_profile (
    account_id UUID PRIMARY KEY,
    qualification_level INTEGER,
    indicators JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE public.point_balances (
    account_id UUID PRIMARY KEY,
    points NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (points >= 0),
    earned_total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    historic_bonus_ceiling NUMERIC(14, 2) NOT NULL DEFAULT 0,
    last_updated TIMESTAMPTZ,
    last_event_id BIGINT,
    CHECK (earned_total >= points)
);

CREATE TABLE public.applied_events (
    event_id BIGINT PRIMARY KEY,
    account_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    base_amount NUMERIC NOT NULL,
    dom_mult NUMERIC NOT NULL,
    qual_mult NUMERIC NOT NULL,
    streak_mult NUMERIC NOT NULL,
    daily_cap NUMERIC NOT NULL,
    raw_amount NUMERIC NOT NULL,
    effective NUMERIC NOT NULL,
    cap_truncated BOOLEAN NOT NULL,
    bonuses_eligible BOOLEAN NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL,
    payload_snapshot JSONB,
    rule_id UUID
);

CREATE TABLE public.typing_daily (
    account_id UUID NOT NULL,
    day DATE NOT NULL,
    points NUMERIC NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, day)
);
CREATE TABLE public.first_payment_guard (
    account_id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Real shape (core/scheduler.py::_refresh_subscribers_snapshot): composite PK,
-- no "active" column — the original ad-hoc /private/tmp setup.sql had this wrong.
CREATE TABLE public.subscribers_snapshot (
    account_id UUID NOT NULL,
    snapshot_date DATE NOT NULL,
    PRIMARY KEY (account_id, snapshot_date)
);
CREATE TABLE public.tier_events (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    from_tier INTEGER NOT NULL,
    to_tier INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE public.redeemed_events (
    payment_id TEXT PRIMARY KEY,
    account_id UUID NOT NULL,
    points_amount NUMERIC(10, 2) NOT NULL CHECK (points_amount > 0),
    discount_rub NUMERIC(10, 2) NOT NULL CHECK (discount_rub >= 0),
    qualification_snapshot TEXT NOT NULL,
    daily_cap_snapshot NUMERIC(8, 2) NOT NULL,
    payment_source TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'confirmed', 'rolled_back')),
    rollback_reason TEXT,
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ,
    rolled_back_at TIMESTAMPTZ,
    metadata JSONB,
    expires_at TIMESTAMPTZ,
    product_code TEXT,
    CHECK ((status = 'confirmed') = (confirmed_at IS NOT NULL)),
    CHECK ((status = 'rolled_back') = (rolled_back_at IS NOT NULL))
);

CREATE TABLE public.redeemed_events_audit (
    audit_id BIGSERIAL PRIMARY KEY,
    payment_id TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL DEFAULT current_user,
    metadata JSONB
);

-- ============================================================
-- Legacy pre-existing audit trigger — plain function, deploy-role-owned,
-- NOT SECURITY DEFINER, no search_path pin. Must exist BEFORE migration 040
-- runs (see module docstring: this is what caught the live ordering bug).
-- ============================================================
CREATE FUNCTION public.fn_log_redeemed_status_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $legacy_audit$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.redeemed_events_audit (payment_id, old_status, new_status, metadata)
        VALUES (NEW.payment_id, NULL, NEW.status, NULL);
    ELSIF OLD.status IS DISTINCT FROM NEW.status
       OR OLD.metadata IS DISTINCT FROM NEW.metadata
       OR OLD.rollback_reason IS DISTINCT FROM NEW.rollback_reason THEN
        INSERT INTO public.redeemed_events_audit (payment_id, old_status, new_status, metadata)
        VALUES (
            NEW.payment_id, OLD.status, NEW.status,
            jsonb_build_object('rollback_reason', NEW.rollback_reason)
        );
    END IF;
    RETURN NEW;
END;
$legacy_audit$;

CREATE TRIGGER trg_redeemed_events_audit
    AFTER INSERT OR UPDATE OF status, metadata, rollback_reason
    ON public.redeemed_events
    FOR EACH ROW
    EXECUTE FUNCTION public.fn_log_redeemed_status_change();
"""


async def _reset_database(conn: asyncpg.Connection) -> None:
    """Wipe this disposable test database back to empty before (re)applying.

    Migration 040's own idempotency check (`migrate_if_needed`) looks at
    `pg_roles` for `points_redeemer` — a CLUSTER-wide fact, not a per-database
    one. On a persistent local Postgres reused across test runs, a role created
    by a previous run would make `migrate_if_needed` silently skip re-applying
    the SQL to THIS (freshly wiped) database. `DROP OWNED BY ... CASCADE` before
    `DROP ROLE` keeps the role check an accurate proxy for "is this database's
    schema actually in the migrated state" again. Safe only because this targets
    a disposable/dedicated test database by contract (WP547_PG_DSN), never prod.
    """
    for role in ("points_redeemer", "projection_writer_rewards", "rewards_points_engine_owner"):
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        if exists:
            await conn.execute(f'DROP OWNED BY "{role}" CASCADE')

    await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    await conn.execute("CREATE SCHEMA public")
    # A real (never-dropped) "public" schema ships with USAGE granted to the
    # PUBLIC pseudo-role by default (PG15+ only revokes the default CREATE
    # grant, not USAGE) — migration 040 relies on this for objects it makes
    # SECURITY DEFINER + owned by rewards_points_engine_owner (e.g. the audit
    # trigger function), since it only grants that role explicit CREATE, not
    # USAGE. A freshly CREATE SCHEMA'd "public" has neither, so restore it here
    # to match real Postgres/Neon defaults rather than patching the migration.
    await conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
    await conn.execute("DROP SCHEMA IF EXISTS _foreign_reference CASCADE")
    await conn.execute("DROP SCHEMA IF EXISTS _foreign_indicators CASCADE")

    for role in ("points_redeemer", "projection_writer_rewards", "rewards_points_engine_owner"):
        await conn.execute(f'DROP ROLE IF EXISTS "{role}"')


async def _apply_schema_and_migrations(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await _reset_database(conn)
        await conn.execute(_PRESCHEMA_SQL)
    finally:
        await conn.close()

    migration_040 = importlib.import_module("db.migrations.040_wp547_rewards_protected_path")
    migration_041 = importlib.import_module("db.migrations.041_wp547_erase_account_balance")

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        applied_040 = await migration_040.migrate_if_needed(pool)
        if not applied_040:
            raise RuntimeError(
                "migration 040 reported already-applied on a freshly wiped test "
                "database — _reset_database() failed to clear points_redeemer"
            )

        # point_balances/redeemed_events predate migration 040 (owned by
        # neon-migrations, a separate repo not vendored here — see
        # writers-matrix.md "Схема point_balances (миграции 003, 242, 251)").
        # Migration 040/041's SECURITY DEFINER functions run AS
        # rewards_points_engine_owner and only grant that role privileges on
        # objects THEY create — base table access to the pre-existing
        # point_balances is assumed already granted in production by that
        # other repo's setup. Minimal predsxema (consensus 01-peer.md п.5)
        # reproduces that one assumption explicitly instead of vendoring the
        # other repo's migration history. Runs after 040 creates the role.
        async with pool.acquire() as conn:
            await conn.execute(
                "GRANT SELECT, UPDATE, DELETE ON public.point_balances "
                "TO rewards_points_engine_owner"
            )

        applied_041 = await migration_041.migrate_if_needed(pool)
        if not applied_041:
            raise RuntimeError(
                "migration 041 reported already-applied on a freshly wiped test "
                "database — erase_account_balance existed before this test ran"
            )
    finally:
        await pool.close()


@pytest.fixture(scope="module")
def wp547_dsn() -> str:
    assert _DSN  # pytestmark already skipped the module when unset
    asyncio.run(_apply_schema_and_migrations(_DSN))
    return _DSN


# ============================================================
# Helpers shared across tests
# ============================================================


async def _insert_redeemed_event(
    conn: asyncpg.Connection,
    payment_id: str,
    account_id: uuid.UUID,
    amount,
    *,
    confirm: bool = False,
    eligible: bool = False,
) -> None:
    await conn.execute(
        """
        INSERT INTO public.redeemed_events (
            payment_id, account_id, points_amount, discount_rub,
            qualification_snapshot, daily_cap_snapshot,
            payment_source, purpose
        ) VALUES ($1, $2, $3, $3, $4, $5, 'manual', 'COURSE')
        """,
        payment_id, account_id, amount, _ADMIN_QUAL, _ADMIN_CAP,
    )
    if confirm:
        await conn.execute(
            """
            UPDATE public.redeemed_events
            SET status = 'confirmed', confirmed_at = now(), balance_apply_eligible = $2
            WHERE payment_id = $1
            """,
            payment_id, eligible,
        )


async def _cleanup(conn: asyncpg.Connection, account_ids: list[uuid.UUID]) -> None:
    await conn.execute(
        "DELETE FROM public.redeemed_events WHERE account_id = ANY($1::uuid[])", account_ids
    )
    await conn.execute(
        "DELETE FROM public.point_balances WHERE account_id = ANY($1::uuid[])", account_ids
    )


# ============================================================
# B1-B3 — legacy fencing, bridge apply, idempotent replay
# ============================================================


def test_b1_legacy_confirmed_row_is_fenced(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 100, 100, 100)",
                account,
            )
            # legacy row: confirmed WITHOUT balance_apply_eligible opt-in.
            await _insert_redeemed_event(
                conn, f"b1-legacy-{account}", account, 20, confirm=True, eligible=False
            )
            outcome = await conn.fetchval(
                "SELECT public.apply_confirmed_burn_v1($1)", f"b1-legacy-{account}"
            )
            points = await conn.fetchval(
                "SELECT points FROM public.point_balances WHERE account_id = $1", account
            )
            assert outcome == "not_ready", outcome
            assert points == 100, points
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


def test_b2_bridge_debit_and_marker_committed(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        payment_id = f"b2-bridge-{account}"
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 100, 100, 100)",
                account,
            )
            await _insert_redeemed_event(conn, payment_id, account, 30, confirm=True, eligible=True)
            outcome = await conn.fetchval("SELECT public.apply_confirmed_burn_v1($1)", payment_id)
            points = await conn.fetchval(
                "SELECT points FROM public.point_balances WHERE account_id = $1", account
            )
            applied_at = await conn.fetchval(
                "SELECT balance_applied_at FROM public.redeemed_events WHERE payment_id = $1",
                payment_id,
            )
            assert outcome == "applied", outcome
            assert points == 70, points
            assert applied_at is not None
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


def test_b3_replay_is_zero_delta(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        payment_id = f"b3-replay-{account}"
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 100, 100, 100)",
                account,
            )
            await _insert_redeemed_event(conn, payment_id, account, 30, confirm=True, eligible=True)
            first = await conn.fetchval("SELECT public.apply_confirmed_burn_v1($1)", payment_id)
            second = await conn.fetchval("SELECT public.apply_confirmed_burn_v1($1)", payment_id)
            points = await conn.fetchval(
                "SELECT points FROM public.point_balances WHERE account_id = $1", account
            )
            assert first == "applied", first
            assert second == "already_applied", second
            assert points == 70, points
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


# ============================================================
# B7 (two cases) — terminal manual-review poison rows
# ============================================================


def test_b7_missing_balance_becomes_terminal(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()  # deliberately no point_balances row
        payment_id = f"b7-missing-{account}"
        try:
            await _insert_redeemed_event(conn, payment_id, account, 10, confirm=True, eligible=True)
            outcome = await conn.fetchval("SELECT public.apply_confirmed_burn_v1($1)", payment_id)
            row = await conn.fetchrow(
                "SELECT balance_apply_failed_at, balance_apply_attempts, balance_apply_last_error "
                "FROM public.redeemed_events WHERE payment_id = $1",
                payment_id,
            )
            assert outcome == "manual_review_balance_missing", outcome
            assert row["balance_apply_failed_at"] is not None
            assert row["balance_apply_attempts"] == 1
            assert row["balance_apply_last_error"] == "balance_missing"
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


def test_b7_insufficient_balance_becomes_terminal(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        payment_id = f"b7-insufficient-{account}"
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 5, 5, 5)",
                account,
            )
            await _insert_redeemed_event(conn, payment_id, account, 10, confirm=True, eligible=True)
            outcome = await conn.fetchval("SELECT public.apply_confirmed_burn_v1($1)", payment_id)
            row = await conn.fetchrow(
                "SELECT balance_apply_failed_at, balance_apply_last_error "
                "FROM public.redeemed_events WHERE payment_id = $1",
                payment_id,
            )
            assert outcome == "manual_review_insufficient_balance", outcome
            assert row["balance_apply_failed_at"] is not None
            assert row["balance_apply_last_error"] == "insufficient_balance"
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


# ============================================================
# B8 — forced marker-write failure rolls back the debit atomically
# ============================================================


def test_b8_forced_marker_failure_rolls_back_debit(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        payment_id = f"b8-rollback-{account}"
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 50, 50, 50)",
                account,
            )
            await _insert_redeemed_event(conn, payment_id, account, 10, confirm=True, eligible=True)

            await conn.execute(
                """
                CREATE OR REPLACE FUNCTION pg_temp.fail_burn_marker()
                RETURNS TRIGGER LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.balance_applied_at IS NOT NULL THEN
                        RAISE EXCEPTION 'forced marker failure';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
            await conn.execute(
                """
                CREATE TRIGGER test_fail_burn_marker
                BEFORE UPDATE OF balance_applied_at ON public.redeemed_events
                FOR EACH ROW EXECUTE FUNCTION pg_temp.fail_burn_marker()
                """
            )
            try:
                with pytest.raises(asyncpg.PostgresError, match="forced marker failure"):
                    await conn.fetchval("SELECT public.apply_confirmed_burn_v1($1)", payment_id)
            finally:
                # asyncpg aborts the implicit tx on error — new connection to keep asserting.
                await conn.close()
                conn = await asyncpg.connect(wp547_dsn)

            points = await conn.fetchval(
                "SELECT points FROM public.point_balances WHERE account_id = $1", account
            )
            applied_at = await conn.fetchval(
                "SELECT balance_applied_at FROM public.redeemed_events WHERE payment_id = $1",
                payment_id,
            )
            assert points == 50, points
            assert applied_at is None
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


# ============================================================
# B9 — bot role has no forbidden direct writes
# ============================================================


def test_b9_bot_direct_balance_marker_audit_writes_denied(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        try:
            forbidden = await conn.fetchval(
                """
                SELECT
                    has_any_column_privilege('points_redeemer', 'public.point_balances', 'UPDATE')
                    OR has_column_privilege(
                        'points_redeemer', 'public.redeemed_events', 'balance_applied_at', 'UPDATE'
                    )
                    OR has_table_privilege('points_redeemer', 'public.redeemed_events_audit', 'INSERT')
                    OR has_sequence_privilege(
                        'points_redeemer', 'public.redeemed_events_audit_audit_id_seq', 'USAGE'
                    )
                """
            )
            assert forbidden is False, "points_redeemer has a forbidden balance/marker/audit write"
        finally:
            await conn.close()

    asyncio.run(run())


# ============================================================
# B10 — compute_available_for_burn stays conservative across states
# ============================================================


def test_b10_available_for_burn_is_conservative(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 100, 100, 100)",
                account,
            )
            await _insert_redeemed_event(
                conn, f"b10-reserve-{account}", account, 10, confirm=False
            )
            pending_id = f"b10-pending-{account}"
            await _insert_redeemed_event(conn, pending_id, account, 15, confirm=True, eligible=True)

            available = await conn.fetchval(
                "SELECT public.compute_available_for_burn($1)", account
            )
            assert available == 75, available  # 100 - 10 reserved - 15 pending-confirmed

            outcome = await conn.fetchval("SELECT public.apply_confirmed_burn_v1($1)", pending_id)
            available_after = await conn.fetchval(
                "SELECT public.compute_available_for_burn($1)", account
            )
            assert outcome == "applied", outcome
            assert available_after == 75, available_after  # reserved 10 + applied debit already reflected in points
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


# ============================================================
# B12 — bot cannot rewrite terminal ledger identity/state
# ============================================================


def test_b12_lifecycle_guard_rejects_identity_and_state_rewrite(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        payment_id = f"b12-terminal-{account}"
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 100, 100, 100)",
                account,
            )
            await _insert_redeemed_event(conn, payment_id, account, 20, confirm=True, eligible=False)

            await conn.execute("SET ROLE points_redeemer")
            try:
                with pytest.raises(asyncpg.PostgresError, match="payment_id is immutable"):
                    await conn.execute(
                        "UPDATE public.redeemed_events SET payment_id = $2 WHERE payment_id = $1",
                        payment_id, f"{payment_id}-renamed",
                    )
            finally:
                await conn.close()
                conn = await asyncpg.connect(wp547_dsn)
                await conn.execute("SET ROLE points_redeemer")

            try:
                with pytest.raises(asyncpg.PostgresError, match="invalid bot ledger transition"):
                    await conn.execute(
                        "UPDATE public.redeemed_events SET status = 'rolled_back', "
                        "rolled_back_at = now() WHERE payment_id = $1",
                        payment_id,
                    )
            finally:
                await conn.close()
                conn = await asyncpg.connect(wp547_dsn)
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


# ============================================================
# B13 — role-bridge apply writes through the protected audit path
# ============================================================


def test_b13_role_bridge_apply_writes_audit(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        payment_id = f"b13-role-bridge-{account}"
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 100, 100, 100)",
                account,
            )

            await conn.execute("SET ROLE points_redeemer")
            await _insert_redeemed_event(conn, payment_id, account, 5, confirm=True, eligible=True)
            outcome = await conn.fetchval("SELECT public.apply_confirmed_burn_v1($1)", payment_id)
            await conn.execute("RESET ROLE")

            assert outcome == "applied", outcome
            has_audit = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM public.redeemed_events_audit WHERE payment_id = $1)",
                payment_id,
            )
            assert has_audit is True
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


# ============================================================
# B4-B6 — concurrency, independent connections, committed setup
# ============================================================


def test_b4_concurrent_apply_debited_exactly_once(wp547_dsn):
    async def run():
        admin = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        payment_id = f"b4-concurrent-{account}"
        try:
            await admin.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 100, 100, 100)",
                account,
            )
            await _insert_redeemed_event(admin, payment_id, account, 30, confirm=True, eligible=True)

            async def apply_once():
                conn = await asyncpg.connect(wp547_dsn)
                try:
                    return await conn.fetchval("SELECT public.apply_confirmed_burn_v1($1)", payment_id)
                finally:
                    await conn.close()

            outcomes = await asyncio.gather(apply_once(), apply_once())
            points = await admin.fetchval(
                "SELECT points FROM public.point_balances WHERE account_id = $1", account
            )
            attempts = await admin.fetchval(
                "SELECT balance_apply_attempts FROM public.redeemed_events WHERE payment_id = $1",
                payment_id,
            )
            assert sorted(outcomes) == ["already_applied", "applied"], outcomes
            assert points == 70, points
            assert attempts == 1, attempts
        finally:
            await _cleanup(admin, [account])
            await admin.close()

    asyncio.run(run())


def test_b6_concurrent_over_reserve_allows_exactly_one(wp547_dsn):
    async def run():
        admin = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        try:
            await admin.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 60, 60, 60)",
                account,
            )

            async def reserve_once(payment_id, amount):
                conn = await asyncpg.connect(wp547_dsn)
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended("
                            "'wp547:account:' || $1::uuid::text, 0))",
                            account,
                        )
                        balance = await conn.fetchval(
                            "SELECT points FROM public.point_balances WHERE account_id = $1 FOR UPDATE",
                            account,
                        )
                        pending = await conn.fetchval(
                            "SELECT COALESCE(SUM(points_amount), 0) FROM public.redeemed_events "
                            "WHERE account_id = $1 AND (status = 'reserved' OR "
                            "(status = 'confirmed' AND balance_apply_eligible AND balance_applied_at IS NULL))",
                            account,
                        )
                        if balance - pending < amount:
                            return False
                        await _insert_redeemed_event(conn, payment_id, account, amount)
                        return True
                finally:
                    await conn.close()

            results = await asyncio.gather(
                reserve_once(f"b6-a-{account}", 60),
                reserve_once(f"b6-b-{account}", 60),
            )
            reserved = await admin.fetchval(
                "SELECT COALESCE(SUM(points_amount), 0) FROM public.redeemed_events "
                "WHERE account_id = $1 AND status = 'reserved'",
                account,
            )
            assert sorted(results) == [False, True], results
            assert reserved == 60, reserved
        finally:
            await _cleanup(admin, [account])
            await admin.close()

    asyncio.run(run())


def test_b5_grant_and_burn_serialize_on_account_lock(wp547_dsn):
    async def run():
        admin = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        payment_id = f"b5-grant-burn-{account}"
        try:
            await admin.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 100, 100, 100)",
                account,
            )
            await _insert_redeemed_event(admin, payment_id, account, 20, confirm=True, eligible=True)

            grant_started = asyncio.Event()

            async def grant_holding_account_lock():
                conn = await asyncpg.connect(wp547_dsn)
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended("
                            "'wp547:account:' || $1::uuid::text, 0))",
                            account,
                        )
                        await conn.execute(
                            "UPDATE public.point_balances SET points = points + 10, "
                            "earned_total = earned_total + 10 WHERE account_id = $1",
                            account,
                        )
                        grant_started.set()
                        await asyncio.sleep(0.25)
                finally:
                    await conn.close()

            async def apply_burn():
                conn = await asyncpg.connect(wp547_dsn)
                try:
                    return await conn.fetchval(
                        "SELECT public.apply_confirmed_burn_v1($1)", payment_id
                    )
                finally:
                    await conn.close()

            grant_task = asyncio.create_task(grant_holding_account_lock())
            await grant_started.wait()
            started_at = time.monotonic()
            burn_outcome = await apply_burn()
            elapsed = time.monotonic() - started_at
            await grant_task

            points = await admin.fetchval(
                "SELECT points FROM public.point_balances WHERE account_id = $1", account
            )
            assert burn_outcome == "applied", burn_outcome
            assert points == 90, points  # 100 + 10 grant - 20 burn
            assert elapsed >= 0.20, elapsed  # burn waited out the grant's held account lock
        finally:
            await _cleanup(admin, [account])
            await admin.close()

    asyncio.run(run())


# ============================================================
# GDPR erase path (migration 041) — new this session (plan п.3)
# ============================================================


def test_erase_deletes_balance_row_and_returns_one(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 42, 42, 42)",
                account,
            )
            deleted = await conn.fetchval("SELECT public.erase_account_balance($1)", account)
            remaining = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM public.point_balances WHERE account_id = $1)",
                account,
            )
            assert deleted == 1, deleted
            assert remaining is False
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


def test_erase_is_idempotent_second_call_returns_zero(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 10, 10, 10)",
                account,
            )
            first = await conn.fetchval("SELECT public.erase_account_balance($1)", account)
            second = await conn.fetchval("SELECT public.erase_account_balance($1)", account)
            assert first == 1, first
            assert second == 0, second
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


def test_erase_missing_account_returns_zero(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()  # never had a point_balances row
        try:
            deleted = await conn.fetchval("SELECT public.erase_account_balance($1)", account)
            assert deleted == 0, deleted
        finally:
            await conn.close()

    asyncio.run(run())


def test_erase_null_account_raises(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        try:
            with pytest.raises(asyncpg.PostgresError, match="account_id is required"):
                await conn.fetchval("SELECT public.erase_account_balance(NULL::uuid)")
        finally:
            await conn.close()

    asyncio.run(run())


def test_erase_acl_points_redeemer_cannot_delete_directly(wp547_dsn):
    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        try:
            can_delete_directly = await conn.fetchval(
                "SELECT has_table_privilege('points_redeemer', 'public.point_balances', 'DELETE')"
            )
            can_call_erase = await conn.fetchval(
                "SELECT has_function_privilege('points_redeemer', "
                "'public.erase_account_balance(uuid)', 'EXECUTE')"
            )
            assert can_delete_directly is False
            assert can_call_erase is True
        finally:
            await conn.close()

    asyncio.run(run())


def test_erase_role_bridge_call_deletes_under_points_redeemer(wp547_dsn):
    """points_redeemer cannot DELETE point_balances directly (ACL test above) but
    CAN call the SECURITY DEFINER function — this is the actual call path profile.py
    uses (rewards pool authenticates as points_redeemer)."""

    async def run():
        conn = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        try:
            await conn.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 15, 15, 15)",
                account,
            )
            await conn.execute("SET ROLE points_redeemer")
            deleted = await conn.fetchval("SELECT public.erase_account_balance($1)", account)
            await conn.execute("RESET ROLE")
            assert deleted == 1, deleted
        finally:
            await _cleanup(conn, [account])
            await conn.close()

    asyncio.run(run())


def test_erase_serializes_with_concurrent_burn_on_same_account(wp547_dsn):
    """erase_account_balance takes the SAME account advisory lock as
    apply_confirmed_burn_v1 (wp547:account:<uuid>) — a GDPR delete cannot
    interleave with a concurrent burn on the same account (01-peer.md Codex,
    ход 1, п.4: "иначе GDPR-delete может конкурировать с apply_confirmed_burn_v1").
    """

    async def run():
        admin = await asyncpg.connect(wp547_dsn)
        account = uuid.uuid4()
        payment_id = f"erase-burn-race-{account}"
        try:
            await admin.execute(
                "INSERT INTO public.point_balances (account_id, points, earned_total, "
                "historic_bonus_ceiling) VALUES ($1, 100, 100, 100)",
                account,
            )
            await _insert_redeemed_event(admin, payment_id, account, 20, confirm=True, eligible=True)

            lock_held = asyncio.Event()

            async def hold_account_lock():
                conn = await asyncpg.connect(wp547_dsn)
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended("
                            "'wp547:account:' || $1::uuid::text, 0))",
                            account,
                        )
                        lock_held.set()
                        await asyncio.sleep(0.25)
                finally:
                    await conn.close()

            async def erase():
                conn = await asyncpg.connect(wp547_dsn)
                try:
                    return await conn.fetchval("SELECT public.erase_account_balance($1)", account)
                finally:
                    await conn.close()

            lock_task = asyncio.create_task(hold_account_lock())
            await lock_held.wait()
            started_at = time.monotonic()
            deleted = await erase()
            elapsed = time.monotonic() - started_at
            await lock_task

            assert deleted == 1, deleted
            assert elapsed >= 0.20, elapsed  # erase waited out the held account lock
        finally:
            await _cleanup(admin, [account])
            await admin.close()

    asyncio.run(run())
