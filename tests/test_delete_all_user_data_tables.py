"""
Tests for db/queries/profile.py — OPTIONAL_CHAT_TABLES used by delete_all_user_data().

No live DB required: validates the table/column identifiers statically, the
same way asyncpg would reject them at execute() time if malformed.

Run: python3 -m pytest tests/test_delete_all_user_data_tables.py -v
"""

import asyncio
import inspect
import os
import sys

import asyncpg
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db.connection as db_connection
from db.queries import profile as profile_queries
from db.queries.profile import (
    IncompleteUserDataDeletion,
    OPTIONAL_CHAT_TABLES,
    _delete_tolerant,
    delete_all_user_data,
)
from db.sql_helpers import delete_from


def test_every_entry_builds_valid_sql():
    for table, column in OPTIONAL_CHAT_TABLES:
        sql = delete_from(f"public.{table}", f"{column} = $1")
        assert sql == f"DELETE FROM public.{table} WHERE {column} = $1"


def test_dynamic_sql_rejects_unqualified_tables():
    with pytest.raises(ValueError, match="Invalid qualified SQL identifier"):
        delete_from("training_setting", "chat_id = $1")


def test_no_duplicate_tables():
    tables = [table for table, _column in OPTIONAL_CHAT_TABLES]
    assert len(tables) == len(set(tables))


def test_community_members_filters_by_telegram_id_not_chat_id():
    """community_members.chat_id is the GROUP chat (db/queries/workshop.py:
    log_community_join/get_community_stats), not the member's own id — the
    member is telegram_id. Filtering by chat_id=<own id> matches zero rows
    (Telegram group ids are negative, user ids positive): a silent no-op
    that leaves the member's row (username, first_name) undeleted."""
    mapping = dict(OPTIONAL_CHAT_TABLES)
    assert mapping['community_members'] == 'telegram_id'


def test_channel_monitors_is_in_optional_block_not_core_transaction():
    """Regression test for the 2026-08-07 prod incident: delete_all_user_data
    crashed for every user with asyncpg.exceptions.UndefinedTableError on
    channel_monitors (legacy, main pool — migrated to channel_monitor,
    singular, publication pool, WP-253; the main-pool table doesn't exist).

    First fix attempt wrapped the DELETE in its own try/except *inside* the
    core transaction (kept there under the belief that a live FK on
    public.users(id) required deleting it before users). Cold-context review
    caught that this doesn't work: PostgreSQL marks the whole transaction
    aborted server-side after the first error, so the very next (unguarded)
    statement in the same transaction — daily_activity_marker — would raise
    asyncpg.exceptions.InFailedSQLTransactionError and crash the function
    exactly the same way, just under a different exception name. Verified
    empirically against a live Postgres 16 + asyncpg instance.

    A dead table holds no FK on anything, so the original "must stay in the
    core transaction for FK ordering" premise was false to begin with —
    OPTIONAL_CHAT_TABLES (separate connection, autocommit per statement,
    one table's failure can't abort another) is the correct home, same as
    the other 18 legacy/migrated tables already there."""
    tables = [table for table, _column in OPTIONAL_CHAT_TABLES]
    assert 'channel_monitors' in tables
    assert dict(OPTIONAL_CHAT_TABLES)['channel_monitors'] == 'chat_id'

    # Confirm it is NOT also duplicated inside delete_all_user_data's core
    # transaction (the bug this test guards against).
    source = inspect.getsource(delete_all_user_data)
    core_transaction_source = source.split("async with conn.transaction():")[1]
    assert "'channel_monitors'" not in core_transaction_source


def test_request_traces_legacy_is_outside_core_transaction():
    """Same bug class and same fix as channel_monitors above, found in the
    same 2026-08-07 review pass: DELETE FROM request_traces (main pool,
    "will be DROPPED after soak") lived inside the core transaction with its
    own try/except — same non-fix, since prod runs with SKIP_DB_MIGRATIONS=true
    (bot.py) so db/models.py's CREATE TABLE is not a reliable signal the table
    still exists. Moved next to channel_monitors, outside the transaction, kept
    under its own result key ('request_traces_legacy') because the health-pool
    copy of this table (migrated destination, WP-253 G4) writes
    result['request_traces'] further down in the same function — colliding
    keys would silently drop one of the two counts."""
    source = inspect.getsource(delete_all_user_data)
    pre_transaction_source, core_transaction_source = source.split("async with conn.transaction():", 1)
    assert "request_traces_legacy" in pre_transaction_source
    assert "request_traces_legacy" not in core_transaction_source
    # The health-pool copy still exists further down, keyed without '_legacy'.
    assert "result['request_traces']" in core_transaction_source


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False  # never suppress — same contract as a real SAVEPOINT


class _FakeConn:
    """Minimal conn.execute()/conn.transaction() double for _delete_tolerant.
    raises is the exception instance execute() should raise, or None for success."""

    def __init__(self, raises=None, delete_count=3):
        self.raises = raises
        self.delete_count = delete_count

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, sql, chat_id):
        if self.raises:
            raise self.raises
        return f"DELETE {self.delete_count}"


def test_delete_tolerant_swallows_undefined_table_error():
    """Regression test for the 2026-08-07 17:04 МСК prod incident:
    delete_all_user_data crashed on development.daily_activity_marker right
    after channel_monitors got fixed the same morning — the exact failure
    mode _delete_tolerant exists to prevent."""
    conn = _FakeConn(raises=asyncpg.exceptions.UndefinedTableError('relation "x" does not exist'))
    result = asyncio.run(_delete_tolerant(conn, "DELETE FROM x WHERE chat_id = $1", 1, "x"))
    assert result == 0


def test_delete_tolerant_reraises_other_errors():
    """Agreed with Codex in peer session 2026-08-07-09: swallowing anything
    beyond UndefinedTableError would let a real failure (permissions, FK
    violation, timeout) report a false 'deleted' — worse than an honest crash."""
    conn = _FakeConn(raises=asyncpg.exceptions.InsufficientPrivilegeError("permission denied"))
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        asyncio.run(_delete_tolerant(conn, "DELETE FROM x WHERE chat_id = $1", 1, "x"))


def test_delete_tolerant_returns_parsed_count_on_success():
    conn = _FakeConn(delete_count=5)
    result = asyncio.run(_delete_tolerant(conn, "DELETE FROM x WHERE chat_id = $1", 1, "x"))
    assert result == 5


class _FakeConnTracksTransaction(_FakeConn):
    """Same double as _FakeConn, plus a counter for how many times
    conn.transaction() (the SAVEPOINT) was actually opened."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.transaction_calls = 0

    def transaction(self):
        self.transaction_calls += 1
        return super().transaction()


def test_delete_tolerant_uses_a_savepoint_not_a_bare_execute():
    """Regression guard for a gap an independent review found (2026-08-07,
    session 2026-08-07-09): the tests above only check catch/re-raise
    behavior and stay green even if the nested `async with
    conn.transaction()` (the actual SAVEPOINT) is deleted from
    _delete_tolerant — which reproduces the original prod crash
    (InFailedSQLTransactionError on the next statement in the outer
    transaction), reproduced empirically against real Postgres 16 by the
    reviewer. A bare conn.execute() without the SAVEPOINT would pass every
    other test in this file; only this one catches that specific mutation."""
    conn = _FakeConnTracksTransaction()
    asyncio.run(_delete_tolerant(conn, "DELETE FROM x WHERE chat_id = $1", 1, "x"))
    assert conn.transaction_calls == 1


def test_remaining_transaction_tables_use_delete_tolerant():
    """The 6 tables identified as the same-class risk as channel_monitors
    (bug-2026-08-07-delete-transaction-remaining-unguarded-tables.md) must
    all go through _delete_tolerant, not a bare conn.execute(). The first 3
    share a loop (generic `result[table]` assignment); the other 3 are
    named individually — checked in their actual source shape, not a single
    templated string."""
    source = inspect.getsource(delete_all_user_data)
    assert "for table in ('reminders', 'feedback_reports', 'subscriptions')" in source
    assert "result[table] = await _delete_tolerant" in source
    for key in ('service_usage', 'user_events', 'daily_activity_marker'):
        assert f"result['{key}'] = await _delete_tolerant" in source


def test_user_state_and_users_deletes_are_not_tolerant():
    """Identity deletion must not swallow errors (see _delete_tolerant
    docstring) — a failed DELETE here has to abort the whole operation, not
    silently report a false 'deleted'."""
    source = inspect.getsource(delete_all_user_data)
    assert "result['user_state'] = _parse_delete_count(deleted)" in source
    assert "result['user_state'] = await _delete_tolerant" not in source
    assert "result['users'] = _parse_delete_count(deleted)" in source
    assert "result['users'] = await _delete_tolerant" not in source


def test_daily_activity_marker_deleted_after_user_state():
    """Lock order must match record_active_day() (db/queries/activity.py)
    and _reset_stats() (states/utilities/mydata.py): user_state before
    daily_activity_marker, else a concurrent delete_all_user_data + activity
    write for the same chat_id can deadlock (WP-7 Ф48). Exact SQL fragment
    positions, not generic substring search — table names also appear in
    comments earlier in the function."""
    source = inspect.getsource(delete_all_user_data)
    user_state_pos = source.index("DELETE FROM development.user_state WHERE chat_id = $1")
    marker_pos = source.index("DELETE FROM development.daily_activity_marker WHERE chat_id = $1")
    assert user_state_pos < marker_pos


def test_cross_pool_tables_keyed_by_account_id_not_chat_id():
    """subscription.contract, indicators.calculated_profile and
    rewards.point_balances key by the Ory account UUID (core/access.py,
    core/tier_detector.py, db/queries/rewards.py) — not by chat_id. account_id
    is a uuid column; binding a plain int chat_id to it raises at the asyncpg
    parameter-encoding step, so a chat_id-based DELETE would always no-op
    silently (caught, logged as a warning, reported as 0 rows) — the same
    failure shape as the community_members bug above, confirmed live in a
    peer-session code review with Kimi 2026-08-06 (session
    2026-08-06-16-aist-bot-delete-bugs)."""
    source = inspect.getsource(delete_all_user_data)
    assert "DELETE FROM public.contract WHERE account_id = $1" in source
    assert "DELETE FROM public.calculated_profile WHERE account_id = $1::uuid" in source
    # WP-547 cutover (2026-09-01): points_redeemer lost direct DELETE on
    # point_balances — erasure now goes through the SECURITY DEFINER
    # erase_account_balance() (migration 041), still keyed by account_id.
    assert "SELECT public.erase_account_balance($1::uuid)" in source
    # WP-554 gap (found 2026-09-02, WP-417 Ф-cutover-scope): learning.cp_assessments
    # (стадия развития, db/queries/cp_assessment.py) was entirely missing from this
    # function — the WP-554 PII map had it nowhere, confirmed by direct code read.
    assert "DELETE FROM learning.cp_assessments WHERE account_id = $1::uuid" in source


def test_cp_assessments_deletion_is_gated_by_account_id():
    """Same failure shape as the other account-id legs above: without a verified
    account_id there is nothing to key the DELETE on, so the leg must report 0
    rather than skip binding entirely (which would raise at the asyncpg parameter
    step, same class of bug this file already guards against)."""
    source = inspect.getsource(delete_all_user_data)
    delete_idx = source.index("DELETE FROM learning.cp_assessments")
    # the nearest 'if account_id:' before this DELETE is the one gating it (not a
    # stray one belonging to an earlier unrelated block further up the function)
    guard_idx = source.rindex("if account_id:", 0, delete_idx)
    gated_block = source[guard_idx:delete_idx]
    assert "get_learning_pool" in gated_block
    assert "result['learning_cp_assessments'] = 0" in source


def test_club_account_keyed_by_chat_id_directly():
    """club_account (community pool) is the WP-253 successor of discourse_accounts
    (main pool, in OPTIONAL_CHAT_TABLES above) and — unlike the three tables in
    test_cross_pool_tables_keyed_by_account_id_not_chat_id — keys by chat_id
    directly, no account_id resolution needed (db/queries/discourse.py)."""
    source = inspect.getsource(delete_all_user_data)
    assert "DELETE FROM public.club_account WHERE chat_id = $1" in source


def test_wp253_migrated_pools_not_missed():
    """discourse_accounts/published_posts/scheduled_publications/conversion_events/
    training_settings/training_children/training_progress/training_attempts all have
    WP-253 successor tables in dedicated pools (publication/lead/reference/learning) —
    found by a cold-context code review that caught what both the writer and Kimi
    missed in the peer session: a live child's name (training_child) surviving
    "delete all my data". Confirmed against db/queries/conversion.py,
    db/queries/training.py and db/queries/discourse.py module docstrings."""
    source = inspect.getsource(delete_all_user_data)
    assert "DELETE FROM public.published_post WHERE chat_id = $1" in source
    assert "DELETE FROM public.scheduled_post WHERE chat_id = $1" in source
    assert "DELETE FROM public.conversion_event WHERE chat_id = $1" in source
    assert "DELETE FROM public.training_setting WHERE chat_id = $1" in source
    assert "DELETE FROM public.training_child WHERE chat_id = $1" in source
    assert "'learning.' + table" in source
    assert "'training_progress', 'training_attempt'" in source


class _DeleteConn:
    """Success-path double for the complete multi-pool deletion traversal."""

    def __init__(
        self,
        execute_error_for: str | None = None,
        error: Exception | None = None,
        fetchval_result=0,
        fetchrow_result=None,
    ):
        self.execute_error_for = execute_error_for
        self.error = error
        self.fetchval_result = fetchval_result
        self.fetchrow_result = fetchrow_result
        self.fetchrow_calls = []

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, sql, *args):
        if self.execute_error_for and self.execute_error_for in sql:
            raise self.error or RuntimeError("injected deletion failure")
        return "DELETE 0"

    async def fetchval(self, sql, *args):
        return self.fetchval_result

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self.fetchrow_result


class _AcquireConn:
    def __init__(self, conn=None, error: Exception | None = None):
        self.conn = conn or _DeleteConn()
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.conn

    async def __aexit__(self, *exc_info):
        return False


class _DeletePool:
    def __init__(self, conn=None, acquire_error: Exception | None = None):
        self.conn = conn or _DeleteConn()
        self.acquire_error = acquire_error

    def acquire(self):
        return _AcquireConn(self.conn, self.acquire_error)


def _patch_deletion_pools(
    monkeypatch,
    *,
    community_pool=None,
    main_pool=None,
    persona_pool=None,
    privacy_pool=None,
):
    """Route every pool used by delete_all_user_data() to deterministic doubles."""
    success_pool = _DeletePool()
    main_pool = main_pool or success_pool
    community_pool = community_pool or success_pool
    persona_pool = persona_pool or success_pool
    privacy_pool = privacy_pool or success_pool

    async def main_getter():
        return main_pool

    async def success_getter():
        return success_pool

    async def community_getter():
        return community_pool

    async def persona_getter():
        return persona_pool

    async def privacy_getter():
        return privacy_pool

    monkeypatch.setattr(profile_queries, "get_pool", main_getter)
    monkeypatch.setattr(profile_queries, "get_learning_pool", success_getter)
    monkeypatch.setattr(profile_queries, "get_health_pool", success_getter)
    monkeypatch.setattr(profile_queries, "get_privacy_deletion_pool", privacy_getter)

    for getter_name in (
        "get_secrets_pool",
        "get_subscription_pool",
        "get_indicators_pool",
        "get_rewards_pool",
        "get_publication_pool",
        "get_lead_pool",
        "get_reference_pool",
        "get_fsm_pool",
        "get_journal_pool",
    ):
        monkeypatch.setattr(db_connection, getter_name, success_getter)
    monkeypatch.setattr(db_connection, "get_community_pool", community_getter)
    monkeypatch.setattr(db_connection, "get_persona_pool", persona_getter)


def test_required_pool_failure_raises_with_partial_result(monkeypatch):
    """A failed required leg must never return the dict consumed by the success UI."""
    community_pool = _DeletePool(acquire_error=RuntimeError("community unavailable"))
    _patch_deletion_pools(monkeypatch, community_pool=community_pool)

    with pytest.raises(IncompleteUserDataDeletion) as error:
        asyncio.run(delete_all_user_data(123456))

    assert error.value.failed_components == ("community.club_account",)
    assert "users" in error.value.partial_result
    assert "123456" not in str(error.value)


def test_persona_identity_lookup_failure_blocks_success(monkeypatch):
    """A missing account-id lookup makes account-keyed legs unverifiable."""
    persona_pool = _DeletePool(acquire_error=RuntimeError("persona unavailable"))
    _patch_deletion_pools(monkeypatch, persona_pool=persona_pool)

    with pytest.raises(IncompleteUserDataDeletion) as error:
        asyncio.run(delete_all_user_data(123456))

    assert error.value.failed_components == ("persona.identity_lookup",)


def test_journal_erasure_uses_verified_account_and_narrow_pool(monkeypatch):
    """The bot passes only persona's verified account ID to the narrow role."""
    account_id = "00000000-0000-0000-0000-000000000001"
    persona_conn = _DeleteConn(fetchval_result=account_id)
    privacy_conn = _DeleteConn(
        fetchrow_result={
            "rows_unlinked": 2,
            "rows_payload_scrubbed": 3,
            "tombstone_external_id": "forget-test",
        }
    )
    _patch_deletion_pools(
        monkeypatch,
        persona_pool=_DeletePool(conn=persona_conn),
        privacy_pool=_DeletePool(conn=privacy_conn),
    )

    result = asyncio.run(delete_all_user_data(123456))

    assert result["journal_domain_event"] == 5
    assert len(privacy_conn.fetchrow_calls) == 1
    sql, args = privacy_conn.fetchrow_calls[0]
    assert "domain_event_forget_account" in sql
    assert args == (account_id, "self_service_account_deletion")


def test_journal_erasure_permission_failure_blocks_success(monkeypatch):
    """Losing EXECUTE must not let the user receive a false completion."""
    persona_conn = _DeleteConn(
        fetchval_result="00000000-0000-0000-0000-000000000001"
    )
    privacy_pool = _DeletePool(
        acquire_error=asyncpg.exceptions.InsufficientPrivilegeError("permission denied")
    )
    _patch_deletion_pools(
        monkeypatch,
        persona_pool=_DeletePool(conn=persona_conn),
        privacy_pool=privacy_pool,
    )

    with pytest.raises(IncompleteUserDataDeletion) as error:
        asyncio.run(delete_all_user_data(123456))

    assert "journal.domain_event" in error.value.failed_components
    assert error.value.partial_result["journal_domain_event"] == 0


def test_privacy_pool_has_no_broad_database_fallback():
    """A missing narrow credential must fail closed instead of using learning access."""
    source = inspect.getsource(db_connection.get_privacy_deletion_pool)

    assert "PRIVACY_DELETION_URL" in source
    assert "LEARNING_URL" not in source


def test_missing_classified_optional_table_does_not_fail_deletion(monkeypatch):
    """Only an explicitly optional table's UndefinedTableError is a safe skip."""
    main_conn = _DeleteConn(
        execute_error_for="public.assessments",
        error=asyncpg.exceptions.UndefinedTableError("relation does not exist"),
    )
    _patch_deletion_pools(monkeypatch, main_pool=_DeletePool(conn=main_conn))

    result = asyncio.run(delete_all_user_data(123456))

    assert result["assessments"] == 0


def test_optional_table_permission_error_blocks_success(monkeypatch):
    """Optional means schema-optional, not permission/error-optional."""
    main_conn = _DeleteConn(
        execute_error_for="public.assessments",
        error=asyncpg.exceptions.InsufficientPrivilegeError("permission denied"),
    )
    _patch_deletion_pools(monkeypatch, main_pool=_DeletePool(conn=main_conn))

    with pytest.raises(IncompleteUserDataDeletion) as error:
        asyncio.run(delete_all_user_data(123456))

    assert "main.assessments" in error.value.failed_components
