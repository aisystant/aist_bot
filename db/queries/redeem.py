from __future__ import annotations

"""
Burn-эмиттер баллов (WP-327 Ф1+Ф2, see DP.SC.141, DP.ROLE.051).

Двухфазный коммит для зачёта баллов в оплату:
  1. reserve_burn() — резерв при чекауте (ДО YooKassa.create_payment / TG send_invoice)
  2. confirm_burn() — подтверждение при payment.succeeded webhook / successful_payment
  3. rollback_burn() — откат при payment.canceled ИЛИ timeout 30 мин

Не writer point_balances — пишет только в rewards.redeemed_events.
События эмитируются ЧЕРЕЗ event-gateway (helpers.dual_write.post_event), НЕ direct INSERT
в learning.domain_event — соответствие DP.SC.020 OwnerIntegrity (single writer = DP.ROLE.032).

Идемпотентность: PK на payment_id, INSERT ... ON CONFLICT DO NOTHING.
Late-webhook handling: confirm_burn после rollback'а → alert event 'points_redeem_late_webhook'.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import asyncpg

from db.connection import get_rewards_pool
from helpers.dual_write import post_event

logger = logging.getLogger(__name__)

# Курс конвертации (TODO: вынести в config / актуализировать через DP.ECON.001 §6 update)
POINTS_TO_RUB_RATE = Decimal("0.875")  # 1 балл = 1¢ × 87.5 ₽/$ (курс на 17 мая 2026)

# Timeout для автоматического rollback резерва (минут)
RESERVATION_TIMEOUT_MIN = 30

# Fallback квалификация для пилотов без записи в indicators.calculated_profile
FALLBACK_QUALIFICATION = "ученик"
FALLBACK_DAILY_CAP = Decimal("100")
FALLBACK_MULTIPLIER = Decimal("1.0")


async def available_discount(
    account_id: str,
    requested_amount_rub: Decimal,
) -> dict:
    """Сколько скидки доступно пилоту для покупки на requested_amount_rub.

    Args:
        account_id: Ory UUID пилота
        requested_amount_rub: целевая сумма покупки в рублях

    Returns:
        {
            "copilka_pts": Decimal,         # текущий баланс пилота
            "ceiling_pts": Decimal,         # daily_cap по квалификации
            "available_pts": Decimal,       # min(copilka, ceiling - reserved_today, requested/rate)
            "discount_rub": Decimal,        # available_pts × POINTS_TO_RUB_RATE
            "qualification": str,           # 'ученик' / 'работник' / ... / 'общественный_деятель'
            "payable_rub": Decimal,         # requested - discount_rub
        }
    """
    pool = await get_rewards_pool()

    # Валидация входов (VR-review #3)
    requested_amount_rub = Decimal(str(requested_amount_rub))
    if requested_amount_rub <= 0:
        raise ValueError(f"requested_amount_rub must be positive, got {requested_amount_rub}")
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, AttributeError) as e:
        raise ValueError(f"Invalid account_id (not a UUID): {account_id!r}") from e

    async with pool.acquire() as conn:
        # 1. Баланс пилота
        balance_row = await conn.fetchrow(
            "SELECT COALESCE(points, 0) AS balance FROM public.point_balances WHERE account_id = $1",
            account_uuid,
        )
        copilka_pts = Decimal(str(balance_row["balance"])) if balance_row else Decimal("0")

        # 2. Степень МИМ через FDW _foreign_indicators
        # qualification_level: INTEGER 1-11. NULL → fallback 'ученик'.
        qual_row = await conn.fetchrow(
            "SELECT qualification_level FROM _foreign_indicators.calculated_profile WHERE account_id = $1",
            account_uuid,
        )
        qual_level = qual_row["qualification_level"] if qual_row else None

        # 3. Resolve level → qualification text + multiplier + daily_cap через FDW _foreign_reference
        # Уровни 9-11 не имеют записей → fallback на ст. 8 (общественный_деятель ×5.0 cap=1000)
        # Уровни 1-3 → ученик/работник/стратег (по sort_order)
        if qual_level is None:
            qualification = FALLBACK_QUALIFICATION
            ceiling_pts = FALLBACK_DAILY_CAP
        else:
            # Сначала ищем точное соответствие через qualification_level (level INT → qualification TEXT)
            qmap_row = await conn.fetchrow(
                """
                SELECT q.qualification, q.daily_cap
                FROM _foreign_reference.qualification_level ql
                JOIN _foreign_reference.qualification_multipliers q
                  ON q.qualification = ql.qualification
                WHERE ql.level = $1
                """,
                qual_level,
            )
            if qmap_row:
                qualification = qmap_row["qualification"]
                ceiling_pts = Decimal(str(qmap_row["daily_cap"]))
            else:
                # Уровни 9-11 → fallback на ст. 8 'общественный_деятель'
                logger.warning(
                    f"[Redeem] qualification_level={qual_level} not in qualification_multipliers → fallback to 'общественный_деятель'"
                )
                qualification = "общественный_деятель"
                ceiling_pts = Decimal("1000")

        # 4. Уже зарезервированное сегодня (через helper из миграции 226)
        avail_row = await conn.fetchrow(
            "SELECT public.compute_available_for_burn($1) AS available",
            account_uuid,
        )
        balance_minus_reserved = Decimal(str(avail_row["available"]))

        # 5. Effective available = min(balance_minus_reserved, ceiling, requested/rate)
        max_by_request = requested_amount_rub / POINTS_TO_RUB_RATE
        available_pts = min(balance_minus_reserved, ceiling_pts, max_by_request)
        available_pts = available_pts.quantize(Decimal("0.01"))

        discount_rub = (available_pts * POINTS_TO_RUB_RATE).quantize(Decimal("0.01"))
        payable_rub = (requested_amount_rub - discount_rub).quantize(Decimal("0.01"))

    logger.info(
        f"[Redeem] available_discount: account={account_id[:8]}, requested={requested_amount_rub}, "
        f"qual={qualification}, balance={copilka_pts}, ceiling={ceiling_pts}, available={available_pts}, discount={discount_rub}"
    )

    return {
        "copilka_pts": copilka_pts,
        "ceiling_pts": ceiling_pts,
        "available_pts": available_pts,
        "discount_rub": discount_rub,
        "qualification": qualification,
        "payable_rub": payable_rub,
    }


async def reserve_burn(
    account_id: str,
    payment_id: str,
    points_amount: Decimal,
    payment_source: str,
    purpose: str,
    qualification_snapshot: str,
    daily_cap_snapshot: Decimal,
) -> bool:
    """Резерв баллов перед оплатой (status='reserved').

    Двухфазный коммит: SELECT FOR UPDATE на point_balances → проверка available → INSERT.

    Args:
        account_id: Ory UUID
        payment_id: ЮКасса payment.id ИЛИ TG провизорный UUID ИЛИ "zero_{uuid}" для 100% скидки
        points_amount: сколько баллов резервируется (positive Decimal)
        payment_source: 'yookassa' / 'tg_stars' / 'stripe' / 'manual' / 'zero_payment'
        purpose: 'SEMINAR' / 'SUBSCRIPTION' / 'EVENT'
        qualification_snapshot: степень МИМ на момент резерва
        daily_cap_snapshot: daily_cap на момент резерва (для replay)

    Returns:
        True — резерв создан, False — payment_id уже использован (idempotent) или недостаточно баллов.
    """
    pool = await get_rewards_pool()
    points_amount = Decimal(str(points_amount))
    discount_rub = (points_amount * POINTS_TO_RUB_RATE).quantize(Decimal("0.01"))

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Race protection: SELECT FOR UPDATE на point_balances. Блокирует параллельные reserve_burn для того же account_id.
            balance_row = await conn.fetchrow(
                "SELECT COALESCE(points, 0) AS balance FROM public.point_balances WHERE account_id = $1 FOR UPDATE",
                uuid.UUID(account_id),
            )
            current_balance = Decimal(str(balance_row["balance"])) if balance_row else Decimal("0")

            # Уже зарезервированное сегодня (без 'confirmed' — оно уже отражено в balance через projection)
            reserved_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(points_amount), 0) AS reserved
                FROM public.redeemed_events
                WHERE account_id = $1
                  AND reserved_at >= date_trunc('day', now())
                  AND status = 'reserved'
                """,
                uuid.UUID(account_id),
            )
            reserved_today = Decimal(str(reserved_row["reserved"]))
            available = current_balance - reserved_today

            if available < points_amount:
                logger.warning(
                    f"[Redeem] reserve_burn rejected: account={account_id[:8]}, "
                    f"requested={points_amount}, available={available} (balance={current_balance}, reserved={reserved_today})"
                )
                return False

            # INSERT с ON CONFLICT для idempotency по payment_id
            result = await conn.fetchrow(
                """
                INSERT INTO public.redeemed_events
                    (payment_id, account_id, points_amount, discount_rub,
                     qualification_snapshot, daily_cap_snapshot, payment_source, purpose, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'reserved')
                ON CONFLICT (payment_id) DO NOTHING
                RETURNING payment_id
                """,
                payment_id,
                uuid.UUID(account_id),
                points_amount,
                discount_rub,
                qualification_snapshot,
                Decimal(str(daily_cap_snapshot)),
                payment_source,
                purpose,
            )

            if result is None:
                # ON CONFLICT — отличаем idempotent retry от race с другим резервом (VR-review #4)
                existing = await conn.fetchrow(
                    "SELECT status, account_id FROM public.redeemed_events WHERE payment_id = $1",
                    payment_id,
                )
                if existing and str(existing["account_id"]) == account_id and existing["status"] in ("reserved", "confirmed"):
                    logger.info(f"[Redeem] reserve_burn idempotent retry: payment_id={payment_id}, status={existing['status']}")
                else:
                    logger.warning(
                        f"[Redeem] reserve_burn CONFLICT (possible race or hash collision): payment_id={payment_id}, "
                        f"existing_status={existing['status'] if existing else 'unknown'}"
                    )
                return False

    logger.info(
        f"[Redeem] reserve_burn: account={account_id[:8]}, payment_id={payment_id}, "
        f"points={points_amount}, discount_rub={discount_rub}, qual={qualification_snapshot}"
    )
    return True


async def confirm_burn(payment_id: str) -> bool:
    """Подтверждение резерва после успешной оплаты.

    Args:
        payment_id: ID платежа (из webhook payment.succeeded ИЛИ TG telegram_payment_charge_id)

    Returns:
        True — подтверждено (или уже было подтверждено идемпотентно),
        False — payment_id не найден ИЛИ был rolled_back (late-webhook).
    """
    pool = await get_rewards_pool()

    async with pool.acquire() as conn:
        # ВАЖНО: confirm разделён на ДВЕ транзакции для предотвращения webhook retry-loop
        # при CHECK (points >= 0) violation.
        # Tx1: UPDATE redeemed_events status='confirmed' — committed независимо.
        # Tx2: UPDATE point_balances — если CHECK violation, status уже 'confirmed' и
        # webhook вернёт 200 (не будет ретрая ЮКассы). Admin alert через post_event.
        # (VR-review C-блокер #1, 17 мая 2026)

        row = await conn.fetchrow(
            """
            UPDATE public.redeemed_events
            SET status = 'confirmed', confirmed_at = now()
            WHERE payment_id = $1 AND status = 'reserved'
            RETURNING account_id, points_amount
            """,
            payment_id,
        )

        if row is not None:
            # TODO (Phase 2 refactor): убрать inline UPDATE point_balances и перейти на
            # projection-worker handler для event_type='points_redeemed'. Сейчас inline для
            # закрытия loop'а WP-327 Ф2 — без projection-worker'а баланс не обновится после
            # confirm. См. DP.ROLE.051 §9.
            try:
                await conn.execute(
                    """
                    UPDATE public.point_balances
                    SET points = points - $1, last_updated = now()
                    WHERE account_id = $2
                    """,
                    row["points_amount"],
                    row["account_id"],
                )
            except asyncpg.CheckViolationError as e:
                # CHECK (points >= 0) сработал — баланс изменился между reserve и confirm,
                # списание превышает доступное. redeemed_events.status уже 'confirmed' (Tx1).
                # Webhook вернёт 200, ЮКасса не будет ретраить. Admin alert для ручного refund.
                logger.error(
                    f"[Redeem] confirm_burn negative_balance: payment_id={payment_id}, "
                    f"points={row['points_amount']}, account={str(row['account_id'])[:8]}. "
                    f"Status set to 'confirmed', balance NOT decremented. Admin review needed. Error: {e}"
                )
                asyncio.create_task(
                    post_event(
                        source="aist-bot",
                        external_id=f"redeem_negative_{payment_id}",
                        event_type="points_redeem_negative_balance",
                        schema_version="v1",
                        occurred_at=datetime.now(timezone.utc),
                        account_id=str(row["account_id"]),
                        payload={
                            "payment_id": payment_id,
                            "points_amount": str(row["points_amount"]),
                            "issue": "check_violation_balance_below_zero",
                        },
                    )
                )

        if row is None:
                # Не нашли в status='reserved' — проверяем текущий статус
                existing = await conn.fetchrow(
                    "SELECT status, account_id, points_amount FROM public.redeemed_events WHERE payment_id = $1",
                    payment_id,
                )

                if existing is None:
                    logger.warning(f"[Redeem] confirm_burn: payment_id={payment_id} not found")
                    return False

                if existing["status"] == "confirmed":
                    logger.info(f"[Redeem] confirm_burn idempotent: payment_id={payment_id} already confirmed")
                    return True

                if existing["status"] == "rolled_back":
                    logger.error(
                        f"[Redeem] LATE WEBHOOK: payment_id={payment_id} was rolled_back, but payment succeeded. "
                        f"Admin alert sent via event-gateway. Manual review required."
                    )
                    # Алерт для admin: оплата прошла после rollback'а резерва (deadletter handler)
                    asyncio.create_task(
                        post_event(
                            source="aist-bot",
                            external_id=f"redeem_late_webhook_{payment_id}",
                            event_type="points_redeem_late_webhook",
                            schema_version="v1",
                            occurred_at=datetime.now(timezone.utc),
                            account_id=str(existing["account_id"]),
                            payload={
                                "payment_id": payment_id,
                                "points_amount": str(existing["points_amount"]),
                                "issue": "payment_succeeded_after_rollback",
                            },
                        )
                    )
                    return False

                logger.warning(f"[Redeem] confirm_burn unexpected status: {existing['status']}")
                return False

            # Успешный confirm — эмитируем event для projection-worker
            asyncio.create_task(
                post_event(
                    source="aist-bot",
                    external_id=f"redeem_{payment_id}",
                    event_type="points_redeemed",
                    schema_version="v1",
                    occurred_at=datetime.now(timezone.utc),
                    account_id=str(row["account_id"]),
                    payload={
                        "payment_id": payment_id,
                        "points_amount": str(row["points_amount"]),
                    },
                )
            )

    logger.info(f"[Redeem] confirm_burn: payment_id={payment_id}, points={row['points_amount']}")
    return True


async def rollback_burn(payment_id: str, reason: str = "manual") -> bool:
    """Откат резерва.

    Args:
        payment_id: ID платежа
        reason: 'payment_canceled' / 'timeout_30min' / 'manual'

    Returns:
        True — откачено, False — не найдено или не в status='reserved'.
    """
    pool = await get_rewards_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE public.redeemed_events
            SET status = 'rolled_back', rolled_back_at = now(), rollback_reason = $2
            WHERE payment_id = $1 AND status = 'reserved'
            RETURNING account_id, points_amount
            """,
            payment_id,
            reason,
        )

    if row is None:
        logger.info(f"[Redeem] rollback_burn no-op: payment_id={payment_id} not in 'reserved'")
        return False

    # Reverse event для audit (projection-worker НЕ обрабатывает — баланс не менялся)
    asyncio.create_task(
        post_event(
            source="aist-bot",
            external_id=f"redeem_rollback_{payment_id}",
            event_type="points_burn_rolled_back",
            schema_version="v1",
            occurred_at=datetime.now(timezone.utc),
            account_id=str(row["account_id"]),
            payload={
                "payment_id": payment_id,
                "points_amount": str(row["points_amount"]),
                "reason": reason,
            },
        )
    )

    logger.info(f"[Redeem] rollback_burn: payment_id={payment_id}, points={row['points_amount']}, reason={reason}")
    return True


async def rollback_expired_reservations() -> int:
    """Cron-задача: откатывает резервы старше RESERVATION_TIMEOUT_MIN минут.

    Returns:
        Количество откаченных резервов.
    """
    pool = await get_rewards_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            UPDATE public.redeemed_events
            SET status = 'rolled_back', rolled_back_at = now(), rollback_reason = 'timeout_{RESERVATION_TIMEOUT_MIN}min'
            WHERE status = 'reserved'
              AND reserved_at < now() - interval '{RESERVATION_TIMEOUT_MIN} minutes'
            RETURNING payment_id, account_id, points_amount
            """
        )

    for row in rows:
        asyncio.create_task(
            post_event(
                source="aist-bot",
                external_id=f"redeem_rollback_{row['payment_id']}",
                event_type="points_burn_rolled_back",
                schema_version="v1",
                occurred_at=datetime.now(timezone.utc),
                account_id=str(row["account_id"]),
                payload={
                    "payment_id": row["payment_id"],
                    "points_amount": str(row["points_amount"]),
                    "reason": f"timeout_{RESERVATION_TIMEOUT_MIN}min",
                },
            )
        )

    count = len(rows)
    if count > 0:
        logger.info(f"[Redeem] rollback_expired_reservations: rolled back {count} reservations (>{RESERVATION_TIMEOUT_MIN}min)")
    return count


async def get_redeem_history(account_id: str, limit: int = 10) -> list[dict]:
    """История списаний пилота для UI /points history.

    Returns: список dict'ов с полями payment_id, points_amount, discount_rub,
    purpose, status, reserved_at, confirmed_at, rolled_back_at.
    """
    pool = await get_rewards_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payment_id, points_amount, discount_rub, purpose, payment_source,
                   status, qualification_snapshot, reserved_at, confirmed_at, rolled_back_at, rollback_reason
            FROM public.redeemed_events
            WHERE account_id = $1
            ORDER BY reserved_at DESC
            LIMIT $2
            """,
            uuid.UUID(account_id),
            limit,
        )

    return [dict(row) for row in rows]
