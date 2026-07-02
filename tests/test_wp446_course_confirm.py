"""peer-session 2026-07-02-15 — WP-446 Ф3b: проактивное подтверждение курсовых резервов.

confirm_course_reserves (lazy, Ф3a) и confirm_reserve_by_payment_id (proactive, Ф3b) — два
конкурентных пути подтверждения одного резерва; оба используют условный UPDATE ...
WHERE status='reserved' RETURNING, поэтому второй вызов на уже подтверждённой/откаченной
строке обязан быть no-op (идемпотентность), не двойным списанием.

rollback_expired_reservations (глобальный TTL-откат) и confirm_reserve_by_payment_id делят
один advisory-lock ключ (hashtext('burn_reserve:' || payment_id)) — тест на предикат в SQL
не даёт этой связке молча разойтись при будущей правке.
"""
import os
import sys
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import db.queries.redeem as redeem  # noqa: E402

ACCOUNT_ID = "2f95ffde-a9cf-4992-8cc3-f438a5284f05"
PAYMENT_ID = "aisys-payment-123"


class FakeConn:
    """Мини-фейк asyncpg conn — то же API, что tests/smoke/test_notification_service.py."""

    def __init__(self, fetchrow_result=None, fetch_result=None):
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result if fetch_result is not None else []
        self.executed_sql = []
        self.fetchrow_calls = 0

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return None
            async def __aexit__(self_, *a):
                return False
        return _Tx()

    async def execute(self, sql, *args):
        self.executed_sql.append(sql)
        return "OK"

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls += 1
        if sql.strip().upper().startswith("SELECT PG_ADVISORY"):
            return None
        return self._fetchrow_result

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


def _patch_pool(monkeypatch, conn):
    pool = FakePool(conn)

    async def _get_rewards_pool():
        return pool
    monkeypatch.setattr(redeem, "get_rewards_pool", _get_rewards_pool)


# ─────────────────────── confirm_reserve_by_payment_id ───────────────────────

@pytest.mark.asyncio
async def test_confirm_reserve_by_payment_id_confirms_reserved_row(monkeypatch):
    conn = FakeConn(fetchrow_result={"account_id": uuid.UUID(ACCOUNT_ID), "points_amount": Decimal("50")})
    _patch_pool(monkeypatch, conn)

    points = await redeem.confirm_reserve_by_payment_id(PAYMENT_ID)

    assert points == Decimal("50")
    # deduct-UPDATE на point_balances должен быть выполнен после confirm
    assert any("point_balances" in sql for sql in conn.executed_sql)


@pytest.mark.asyncio
async def test_confirm_reserve_by_payment_id_is_idempotent(monkeypatch):
    """Вторая попытка подтвердить уже confirmed/rolled_back резерв — no-op, не повторное списание."""
    conn = FakeConn(fetchrow_result=None)  # UPDATE...WHERE status='reserved' не заматчил строку
    _patch_pool(monkeypatch, conn)

    points = await redeem.confirm_reserve_by_payment_id(PAYMENT_ID)

    assert points is None
    assert not any("point_balances" in sql for sql in conn.executed_sql)


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
    assert any("point_balances" in sql for sql in conn.executed_sql)


@pytest.mark.asyncio
async def test_confirm_course_reserves_empty_course_codes_is_noop(monkeypatch):
    conn = FakeConn()
    _patch_pool(monkeypatch, conn)

    confirmed = await redeem.confirm_course_reserves(ACCOUNT_ID, [])

    assert confirmed == 0
    assert conn.executed_sql == []  # ранний return — ни одного запроса к БД
