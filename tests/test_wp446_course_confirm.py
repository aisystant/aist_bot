"""peer-session 2026-07-02-15 — WP-446 Ф3b: проактивное подтверждение курсовых резервов.

confirm_course_reserves (lazy, Ф3a) и confirm_reserve_by_payment_id (proactive, Ф3b) — два
конкурентных пути подтверждения одного резерва; оба используют условный UPDATE ...
WHERE status='reserved' RETURNING, поэтому второй вызов на уже подтверждённой/откаченной
строке обязан быть no-op (идемпотентность), не двойным списанием.

rollback_expired_reservations (глобальный TTL-откат) и confirm_reserve_by_payment_id делят
один advisory-lock ключ (hashtext('burn_reserve:' || payment_id)) — тест на предикат в SQL
не даёт этой связке молча разойтись при будущей правке.
"""
import asyncio
import importlib
import logging
import os
import sys
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import db.queries.redeem as redeem  # noqa: E402
import db.queries.rewards as rewards  # noqa: E402
import db.connection as db_connection  # noqa: E402
import core.error_classifier as error_classifier  # noqa: E402
from core.error_classifier import classify_error  # noqa: E402

ACCOUNT_ID = "2f95ffde-a9cf-4992-8cc3-f438a5284f05"
PAYMENT_ID = "aisys-payment-123"


class FakeConn:
    """Мини-фейк asyncpg conn — то же API, что tests/smoke/test_notification_service.py."""

    def __init__(
        self,
        fetchrow_result=None,
        fetchrow_results=None,
        fetch_result=None,
        execute_error=None,
        burn_apply_outcome="applied",
        account_id_result=None,
    ):
        self._fetchrow_result = fetchrow_result
        self._fetchrow_results = list(fetchrow_results or [])
        self._fetch_result = fetch_result if fetch_result is not None else []
        self._execute_error = execute_error
        self._burn_apply_outcome = burn_apply_outcome
        self._account_id_result = (
            uuid.UUID(ACCOUNT_ID) if account_id_result is None else account_id_result
        )
        self.executed_sql = []
        self.fetchrow_calls = 0
        self.transaction_exit_types = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, exc_type, exc, traceback):
                conn.transaction_exit_types.append(exc_type)
                return False
        return _Tx()

    async def execute(self, sql, *args):
        self.executed_sql.append(sql)
        return "OK"

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls += 1
        self.executed_sql.append(sql)
        if sql.strip().upper().startswith("SELECT PG_ADVISORY"):
            return None
        if self._fetchrow_results:
            return self._fetchrow_results.pop(0)
        return self._fetchrow_result

    async def fetchval(self, sql, *args):
        self.executed_sql.append(sql)
        if "apply_confirmed_burn_v1" in sql:
            if self._execute_error:
                raise self._execute_error
            return self._burn_apply_outcome
        if "SELECT account_id" in sql:
            return self._account_id_result
        return None

    async def fetch(self, sql, *args):
        self.executed_sql.append(sql)
        return self._fetch_result


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self_):
                return conn
            async def __aexit__(self_, *a):
                return False
        return _Acq()


class FakeHealthConn:
    def __init__(self, rows):
        self.rows = rows
        self.execute_calls = []

    async def fetch(self, sql, *args):
        if "OFFSET $2" in sql and len(args) == 2:
            page_size, offset = args
            return self.rows[offset:offset + page_size]
        return self.rows

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 0"


def _patch_pool(monkeypatch, conn):
    pool = FakePool(conn)

    async def _get_rewards_pool():
        return pool
    monkeypatch.setattr(redeem, "get_rewards_pool", _get_rewards_pool)


def _patch_reference_pool(monkeypatch, conn):
    pool = FakePool(conn)

    async def _get_reference_pool():
        return pool
    monkeypatch.setattr(rewards, "get_reference_pool", _get_reference_pool)


def _patch_health_pool(monkeypatch, conn):
    pool = FakePool(conn)

    async def _get_health_pool():
        return pool

    monkeypatch.setattr(error_classifier, "get_health_pool", _get_health_pool)


def _deduction_row():
    return {
        "payment_id": PAYMENT_ID,
        "account_id": uuid.UUID(ACCOUNT_ID),
        "points_amount": Decimal("50"),
        "product_code": "course-a",
    }


@pytest.mark.asyncio
async def test_loyalty_rate_uses_configured_value(monkeypatch):
    conn = FakeConn(fetchrow_result={"rate": Decimal("0.10")})
    _patch_reference_pool(monkeypatch, conn)

    assert await rewards.get_loyalty_rate() == Decimal("0.10")


@pytest.mark.asyncio
async def test_loyalty_rate_falls_back_to_current_rate(monkeypatch):
    conn = FakeConn(fetchrow_result=None)
    _patch_reference_pool(monkeypatch, conn)

    assert await rewards.get_loyalty_rate() == Decimal("0.10")


@pytest.mark.asyncio
async def test_rewards_pool_identity_accepts_bridge_role(monkeypatch):
    monkeypatch.setenv("BURN_APPLY_MODE", "bridge")
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "session_user": "points_redeemer",
        "current_user": "points_redeemer",
        "burn_function_exists": True,
        "can_execute_burn": True,
    }

    await db_connection._init_rewards_connection(conn)

    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_rewards_pool_identity_rejects_owner_or_set_role():
    for session_user, current_user in (
        ("neondb_owner", "neondb_owner"),
        ("points_redeemer", "rewards_points_engine_owner"),
    ):
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "session_user": session_user,
            "current_user": current_user,
        }

        with pytest.raises(RuntimeError, match="points_redeemer"):
            await db_connection._init_rewards_connection(conn)


@pytest.mark.asyncio
async def test_rewards_pool_identity_accepts_projection_role_after_revoke(monkeypatch):
    monkeypatch.setenv("BURN_APPLY_MODE", "projection")
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "session_user": "points_redeemer",
        "current_user": "points_redeemer",
        "burn_function_exists": True,
        "can_execute_burn": False,
    }

    await db_connection._init_rewards_connection(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "can_execute", "expected"),
    [
        ("bridge", False, "requires points_redeemer EXECUTE"),
        ("projection", True, "requires points_redeemer EXECUTE.*revoked"),
    ],
)
async def test_rewards_pool_identity_rejects_cutover_acl_mismatch(
    monkeypatch, mode, can_execute, expected
):
    monkeypatch.setenv("BURN_APPLY_MODE", mode)
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "session_user": "points_redeemer",
        "current_user": "points_redeemer",
        "burn_function_exists": True,
        "can_execute_burn": can_execute,
    }

    with pytest.raises(RuntimeError, match=expected):
        await db_connection._init_rewards_connection(conn)


@pytest.mark.asyncio
async def test_rewards_pool_identity_rejects_missing_burn_function(monkeypatch):
    monkeypatch.setenv("BURN_APPLY_MODE", "projection")
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "session_user": "points_redeemer",
        "current_user": "points_redeemer",
        "burn_function_exists": False,
        "can_execute_burn": False,
    }

    with pytest.raises(RuntimeError, match="apply_confirmed_burn_v1 must exist"):
        await db_connection._init_rewards_connection(conn)


def test_burn_apply_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("BURN_APPLY_MODE", "dual-writer")

    with pytest.raises(RuntimeError, match="BURN_APPLY_MODE must be one of"):
        db_connection.get_burn_apply_mode()


@pytest.mark.asyncio
@pytest.mark.parametrize("payment_row_id", [0, 547])
@pytest.mark.parametrize(
    ("module_name", "processor_name", "create_payment_name", "metadata"),
    [
        (
            "handlers.workshop",
            "process_yookassa_webhook",
            "create_and_confirm_payment",
            {"telegram_id": "123"},
        ),
        (
            "handlers.showcase",
            "process_seminar_yookassa_webhook",
            "create_seminar_payment",
            {"telegram_id": "123", "product_code": "seminar-a"},
        ),
    ],
)
async def test_yookassa_burn_failure_requests_webhook_retry(
    monkeypatch,
    payment_row_id,
    module_name,
    processor_name,
    create_payment_name,
    metadata,
):
    module = importlib.import_module(module_name)
    create_payment = AsyncMock(return_value=payment_row_id)
    confirm = AsyncMock(side_effect=RuntimeError("rewards temporarily unavailable"))
    monkeypatch.setattr(module, create_payment_name, create_payment)
    monkeypatch.setattr(module, "confirm_burn", confirm)
    processor = getattr(module, processor_name)
    payload = {
        "event": "payment.succeeded",
        "object": {
            "id": PAYMENT_ID,
            "status": "succeeded",
            "metadata": metadata,
            "amount": {"value": "100.00"},
        },
    }

    with pytest.raises(RuntimeError, match="rewards temporarily unavailable"):
        await processor(payload, AsyncMock())

    confirm.assert_awaited_once_with(PAYMENT_ID)


@pytest.mark.asyncio
async def test_reserve_serializes_account_and_counts_unapplied_confirmed(monkeypatch):
    conn = FakeConn(
        fetchrow_results=[
            {"balance": Decimal("100")},
            {"reserved": Decimal("20")},
            {"payment_id": PAYMENT_ID},
        ]
    )
    _patch_pool(monkeypatch, conn)
    monkeypatch.setattr(
        redeem, "_get_rate", AsyncMock(return_value=Decimal("0.10"))
    )

    created = await redeem.reserve_burn(
        ACCOUNT_ID,
        PAYMENT_ID,
        Decimal("50"),
        "manual",
        "COURSE",
        "ученик",
        Decimal("100"),
    )

    assert created is True
    account_lock_index = next(
        index
        for index, sql in enumerate(conn.executed_sql)
        if "wp547:account:" in sql
    )
    balance_lock_index = next(
        index
        for index, sql in enumerate(conn.executed_sql)
        if "point_balances" in sql and "FOR UPDATE" in sql
    )
    assert account_lock_index < balance_lock_index
    pending_sql = next(
        sql for sql in conn.executed_sql if "SUM(points_amount)" in sql
    )
    assert "balance_apply_eligible" in pending_sql
    assert "balance_applied_at IS NULL" in pending_sql
    insert_sql = next(
        sql for sql in conn.executed_sql if "INSERT INTO public.redeemed_events" in sql
    )
    assert "purpose, status" not in insert_sql
    assert "'reserved'" not in insert_sql


# ─────────────────────── confirm_reserve_by_payment_id ───────────────────────

@pytest.mark.asyncio
async def test_confirm_reserve_by_payment_id_confirms_reserved_row(monkeypatch):
    conn = FakeConn(fetchrow_result={"account_id": uuid.UUID(ACCOUNT_ID), "points_amount": Decimal("50")})
    _patch_pool(monkeypatch, conn)

    points = await redeem.confirm_reserve_by_payment_id(PAYMENT_ID)

    assert points == Decimal("50")
    assert any("apply_confirmed_burn_v1" in sql for sql in conn.executed_sql)
    assert not any("UPDATE public.point_balances" in sql for sql in conn.executed_sql)
    account_lock_index = next(
        index
        for index, sql in enumerate(conn.executed_sql)
        if "wp547:account:" in sql
    )
    confirm_index = next(
        index
        for index, sql in enumerate(conn.executed_sql)
        if "UPDATE public.redeemed_events" in sql
    )
    apply_index = next(
        index
        for index, sql in enumerate(conn.executed_sql)
        if "apply_confirmed_burn_v1" in sql
    )
    assert account_lock_index < confirm_index < apply_index


@pytest.mark.asyncio
async def test_confirm_reserve_by_payment_id_is_idempotent(monkeypatch):
    """Вторая попытка подтвердить уже confirmed/rolled_back резерв — no-op, не повторное списание."""
    conn = FakeConn(fetchrow_result=None)  # UPDATE...WHERE status='reserved' не заматчил строку
    _patch_pool(monkeypatch, conn)

    points = await redeem.confirm_reserve_by_payment_id(PAYMENT_ID)

    assert points is None
    assert not any("point_balances" in sql for sql in conn.executed_sql)


@pytest.mark.parametrize(
    ("operation", "uses_fetchrow", "invoke"),
    [
        (
            "confirm_subscription_reserves",
            False,
            lambda: redeem.confirm_subscription_reserves(ACCOUNT_ID),
        ),
        (
            "confirm_course_reserves",
            False,
            lambda: redeem.confirm_course_reserves(ACCOUNT_ID, ["course-a"]),
        ),
        (
            "confirm_reserve_by_payment_id",
            True,
            lambda: redeem.confirm_reserve_by_payment_id(PAYMENT_ID),
        ),
    ],
)
@pytest.mark.asyncio
async def test_transactional_burn_function_failure_rolls_back_confirmation(
    monkeypatch, caplog, operation, uses_fetchrow, invoke
):
    row = _deduction_row()
    conn = FakeConn(
        fetchrow_result=row if uses_fetchrow else None,
        fetch_result=[] if uses_fetchrow else [row],
        execute_error=RuntimeError("protected burn function failed"),
    )
    _patch_pool(monkeypatch, conn)

    with caplog.at_level(logging.ERROR, logger=redeem.__name__):
        with pytest.raises(RuntimeError, match="protected burn function failed"):
            await invoke()

    assert conn.transaction_exit_types == [RuntimeError]
    assert redeem.DEDUCTION_FAILURE_ERROR_MARKER in caplog.text
    assert f"operation={operation}" in caplog.text
    assert PAYMENT_ID not in caplog.text
    assert ACCOUNT_ID not in caplog.text


@pytest.mark.parametrize(
    ("operation", "uses_fetchrow", "invoke"),
    [
        (
            "confirm_subscription_reserves",
            False,
            lambda: redeem.confirm_subscription_reserves(ACCOUNT_ID),
        ),
        (
            "confirm_course_reserves",
            False,
            lambda: redeem.confirm_course_reserves(ACCOUNT_ID, ["course-a"]),
        ),
        (
            "confirm_reserve_by_payment_id",
            True,
            lambda: redeem.confirm_reserve_by_payment_id(PAYMENT_ID),
        ),
    ],
)
@pytest.mark.asyncio
async def test_transactional_burn_manual_review_commits_durable_state(
    monkeypatch, caplog, operation, uses_fetchrow, invoke
):
    row = _deduction_row()
    conn = FakeConn(
        fetchrow_result=row if uses_fetchrow else None,
        fetch_result=[] if uses_fetchrow else [row],
        burn_apply_outcome="manual_review_balance_missing",
    )
    _patch_pool(monkeypatch, conn)

    with caplog.at_level(logging.ERROR, logger=redeem.__name__):
        await invoke()

    assert conn.transaction_exit_types == [None]
    assert redeem.DEDUCTION_FAILURE_ERROR_MARKER in caplog.text
    assert f"operation={operation}" in caplog.text
    assert "outcome=balance_row_missing" in caplog.text
    assert PAYMENT_ID not in caplog.text
    assert ACCOUNT_ID not in caplog.text


@pytest.mark.asyncio
async def test_confirm_burn_failed_deduction_emits_only_negative_event(monkeypatch, caplog):
    conn = FakeConn(
        fetchrow_result=_deduction_row(),
        burn_apply_outcome="manual_review_insufficient_balance",
    )
    _patch_pool(monkeypatch, conn)
    post_event = AsyncMock()
    monkeypatch.setattr(redeem, "post_event", post_event)

    with caplog.at_level(logging.ERROR, logger=redeem.__name__):
        assert await redeem.confirm_burn(PAYMENT_ID) is True
        await asyncio.sleep(0)

    event_types = [call.kwargs["event_type"] for call in post_event.await_args_list]
    assert event_types == ["points_redeem_negative_balance"]
    assert "points_redeemed" not in event_types
    assert redeem.NEGATIVE_BALANCE_ERROR_MARKER in caplog.text
    assert "operation=confirm_burn" in caplog.text
    assert conn.transaction_exit_types == [None]
    assert not any("UPDATE public.point_balances" in sql for sql in conn.executed_sql)
    assert PAYMENT_ID not in caplog.text
    assert ACCOUNT_ID not in caplog.text


@pytest.mark.asyncio
async def test_confirm_burn_missing_balance_row_emits_failure_only(monkeypatch, caplog):
    conn = FakeConn(
        fetchrow_result=_deduction_row(),
        burn_apply_outcome="manual_review_balance_missing",
    )
    _patch_pool(monkeypatch, conn)
    post_event = AsyncMock()
    monkeypatch.setattr(redeem, "post_event", post_event)

    with caplog.at_level(logging.ERROR, logger=redeem.__name__):
        assert await redeem.confirm_burn(PAYMENT_ID) is True
        await asyncio.sleep(0)

    assert [call.kwargs["event_type"] for call in post_event.await_args_list] == [
        "points_redeem_negative_balance"
    ]
    assert post_event.await_args.kwargs["payload"]["issue"] == (
        "confirmed_without_balance_deduction"
    )
    assert redeem.DEDUCTION_FAILURE_ERROR_MARKER in caplog.text
    assert "outcome=balance_row_missing" in caplog.text
    assert PAYMENT_ID not in caplog.text
    assert ACCOUNT_ID not in caplog.text
    assert conn.transaction_exit_types == [None]
    assert not any("UPDATE public.point_balances" in sql for sql in conn.executed_sql)


@pytest.mark.asyncio
async def test_confirm_burn_success_marks_through_protected_function(monkeypatch):
    monkeypatch.setenv("BURN_APPLY_MODE", "bridge")
    conn = FakeConn(fetchrow_result=_deduction_row())
    _patch_pool(monkeypatch, conn)
    post_event = AsyncMock()
    monkeypatch.setattr(redeem, "post_event", post_event)

    assert await redeem.confirm_burn(PAYMENT_ID) is True
    await asyncio.sleep(0)

    assert conn.transaction_exit_types == [None]
    assert any("apply_confirmed_burn_v1" in sql for sql in conn.executed_sql)
    assert not any("UPDATE public.point_balances" in sql for sql in conn.executed_sql)
    assert [call.kwargs["event_type"] for call in post_event.await_args_list] == [
        "points_redeemed"
    ]


@pytest.mark.asyncio
async def test_confirm_burn_projection_mode_only_enqueues(monkeypatch):
    monkeypatch.setenv("BURN_APPLY_MODE", "projection")
    conn = FakeConn(fetchrow_result=_deduction_row())
    _patch_pool(monkeypatch, conn)
    post_event = AsyncMock()
    monkeypatch.setattr(redeem, "post_event", post_event)

    assert await redeem.confirm_burn(PAYMENT_ID) is True
    await asyncio.sleep(0)

    confirm_sql = next(
        sql
        for sql in conn.executed_sql
        if "UPDATE public.redeemed_events" in sql
    )
    assert "balance_apply_eligible = TRUE" in confirm_sql
    assert not any(
        "apply_confirmed_burn_v1" in sql for sql in conn.executed_sql
    )
    assert conn.transaction_exit_types == [None]
    assert [call.kwargs["event_type"] for call in post_event.await_args_list] == [
        "points_redeemed"
    ]


@pytest.mark.asyncio
async def test_confirm_burn_does_not_opt_in_legacy_confirmed_row(monkeypatch):
    conn = FakeConn(
        fetchrow_results=[
            None,
            {
                "status": "confirmed",
                "account_id": uuid.UUID(ACCOUNT_ID),
                "points_amount": Decimal("50"),
                "balance_apply_eligible": False,
            },
        ]
    )
    _patch_pool(monkeypatch, conn)
    post_event = AsyncMock()
    monkeypatch.setattr(redeem, "post_event", post_event)

    assert await redeem.confirm_burn(PAYMENT_ID) is True

    assert not any("apply_confirmed_burn_v1" in sql for sql in conn.executed_sql)
    post_event.assert_not_awaited()


def test_negative_balance_marker_is_classified_as_critical():
    result = classify_error(
        "db.queries.redeem",
        f"[Redeem] {redeem.NEGATIVE_BALANCE_ERROR_MARKER} operation=confirm_burn",
        None,
    )

    assert result["category"] == "db"
    assert result["severity"] == "L4"
    assert "Manual review" in result["action"]


def test_deduction_failure_marker_is_classified_as_critical():
    result = classify_error(
        "db.queries.redeem",
        f"[Redeem] {redeem.DEDUCTION_FAILURE_ERROR_MARKER} operation=confirm_burn",
        None,
    )

    assert result["category"] == "db"
    assert result["severity"] == "L4"
    assert "Manual review" in result["action"]


def test_external_classifier_redacts_identifiers_and_credentials():
    account_id = "2f95ffde-a9cf-4992-8cc3-f438a5284f05"
    raw = (
        f"account_id={account_id} payment_id=123456789 "
        "pilot@example.com postgresql://admin:secret@db.internal/rewards "
        "Bearer abcdefghijklmnop /Users/alice/IWE @private_handle"
    )

    redacted = error_classifier._redact_for_external(raw, 500)

    for secret in (
        account_id,
        "123456789",
        "pilot@example.com",
        "admin:secret",
        "abcdefghijklmnop",
        "/Users/alice",
        "@private_handle",
    ):
        assert secret not in redacted
    assert "<id>" in redacted
    assert "<email>" in redacted
    assert "<credentials>" in redacted


@pytest.mark.asyncio
async def test_escalation_filters_before_limit_and_acknowledges_after_delivery(monkeypatch):
    rows = [
        {
            "id": row_id,
            "category": "telegram_api",
            "severity": "L3",
            "logger_name": "aiogram",
            "message": "bot was blocked by the user",
            "occurrence_count": 10,
        }
        for row_id in range(1, 6)
    ]
    rows.append(
        {
            "id": 6,
            "category": "db",
            "severity": "L4",
            "logger_name": "db.queries.redeem",
            "message": redeem.DEDUCTION_FAILURE_ERROR_MARKER,
            "occurrence_count": 1,
        }
    )
    conn = FakeHealthConn(rows)
    _patch_health_pool(monkeypatch, conn)

    escalation = await error_classifier.check_escalation()

    assert escalation is not None
    assert escalation.ids == (6,)
    assert redeem.DEDUCTION_FAILURE_ERROR_MARKER in escalation.text
    assert not any(
        "SET escalated = TRUE" in sql for sql, _ in conn.execute_calls
    )

    await error_classifier.mark_escalation_sent(escalation.ids)

    acknowledgement = [
        call for call in conn.execute_calls if "SET escalated = TRUE" in call[0]
    ]
    assert len(acknowledgement) == 1
    assert acknowledgement[0][1] == ([6],)


@pytest.mark.asyncio
async def test_escalation_paginates_past_fifty_suppressed_rows(monkeypatch):
    rows = [
        {
            "id": row_id,
            "category": "telegram_api",
            "severity": "L3",
            "logger_name": "aiogram",
            "message": "bot was blocked by the user",
            "occurrence_count": 10,
        }
        for row_id in range(1, 56)
    ]
    rows.append(
        {
            "id": 56,
            "category": "db",
            "severity": "L4",
            "logger_name": "db.queries.redeem",
            "message": redeem.DEDUCTION_FAILURE_ERROR_MARKER,
            "occurrence_count": 1,
        }
    )
    conn = FakeHealthConn(rows)
    _patch_health_pool(monkeypatch, conn)

    escalation = await error_classifier.check_escalation()

    assert escalation is not None
    assert escalation.ids == (56,)


# ─────────────────────── get_pending_course_reserves ───────────────────────

@pytest.mark.asyncio
async def test_get_pending_course_reserves_maps_rows(monkeypatch):
    conn = FakeConn(fetch_result=[
        {"payment_id": PAYMENT_ID, "account_id": uuid.UUID(ACCOUNT_ID)},
    ])
    _patch_pool(monkeypatch, conn)

    rows = await redeem.get_pending_course_reserves()

    assert rows == [{"payment_id": PAYMENT_ID, "account_id": ACCOUNT_ID}]


# ─────────────────────── rollback_expired_reservations ───────────────────────

@pytest.mark.asyncio
async def test_rollback_expired_reservations_has_advisory_lock_guard(monkeypatch):
    """Регрессия: TTL-откат и confirm_reserve_by_payment_id делят advisory-lock ключ
    (hashtext('burn_reserve:' || payment_id)) — без этого предиката пропадает
    защита от гонки, найденной в peer-сессии 2026-07-02-15 (turn 5)."""
    conn = FakeConn(fetch_result=[])
    _patch_pool(monkeypatch, conn)

    await redeem.rollback_expired_reservations()

    rollback_sql = conn.executed_sql[-1]
    assert "pg_try_advisory_xact_lock" in rollback_sql
    assert "hashtext('burn_reserve:'" in rollback_sql


# ─────────────────────── confirm_course_reserves (Ф3a, ранее без тестов) ───────────────────────

@pytest.mark.asyncio
async def test_confirm_course_reserves_filters_by_product_code(monkeypatch):
    """product_code = ANY($2) — резерв курса A не подтверждается доступом к курсу B (см. docstring)."""
    conn = FakeConn(fetch_result=[
        {"payment_id": PAYMENT_ID, "account_id": uuid.UUID(ACCOUNT_ID),
         "points_amount": Decimal("30"), "product_code": "course-a"},
    ])
    _patch_pool(monkeypatch, conn)

    confirmed = await redeem.confirm_course_reserves(ACCOUNT_ID, ["course-a"])

    assert confirmed == 1
    assert any("apply_confirmed_burn_v1" in sql for sql in conn.executed_sql)
    assert not any("UPDATE public.point_balances" in sql for sql in conn.executed_sql)


@pytest.mark.asyncio
async def test_confirm_course_reserves_empty_course_codes_is_noop(monkeypatch):
    conn = FakeConn()
    _patch_pool(monkeypatch, conn)

    confirmed = await redeem.confirm_course_reserves(ACCOUNT_ID, [])

    assert confirmed == 0
    assert conn.executed_sql == []  # ранний return — ни одного запроса к БД
