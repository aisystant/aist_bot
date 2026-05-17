from __future__ import annotations

"""
Burn-эмиттер баллов (WP-327 Ф1, see DP.SC.141, DP.ROLE.051).

Двухфазный коммит для зачёта баллов в оплату:
  1. reserve_burn() — резерв при чекауте (ДО YooKassa.create_payment)
  2. confirm_burn() — подтверждение при payment.succeeded webhook
  3. rollback_burn() — откат при payment.canceled ИЛИ timeout 30 мин

Не writer point_balances — пишет только в rewards.redeemed_events +
эмитирует event 'points_redeemed' в learning.domain_event для projection-worker.

Идемпотентность: PK на payment_id, INSERT ... ON CONFLICT DO NOTHING.

Skeleton WP-327 Ф1 — заглушки + контракт. Полная реализация в Ф2.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from db.connection import get_pool

logger = logging.getLogger(__name__)

# Курс конвертации (TODO: вынести в config / актуализировать через DP.ECON.001 §6 update)
POINTS_TO_RUB_RATE = Decimal("0.875")  # 1 балл = 1¢ × 87.5 ₽/$ (курс на 17 мая 2026)

# Timeout для автоматического rollback резерва (минут)
RESERVATION_TIMEOUT_MIN = 30


async def available_discount(
    account_id: str,
    requested_amount_rub: int,
) -> dict:
    """Сколько скидки доступно пилоту для покупки на requested_amount_rub.

    Args:
        account_id: Ory UUID пилота
        requested_amount_rub: целевая сумма покупки в рублях

    Returns:
        {
            "copilka_pts": int,             # текущий баланс пилота
            "ceiling_pts": int,             # daily_cap по квалификации
            "available_pts": int,           # min(copilka, ceiling - reserved_today)
            "discount_rub": int,            # available_pts × POINTS_TO_RUB_RATE, capped by requested
            "qualification": str,           # 'ученик' / 'работник' / ... / 'общественный_деятель'
            "payable_rub": int,             # requested - discount (то, что пользователь платит)
        }

    Skeleton: TODO в Ф2 — реальная логика с FDW _foreign_indicators (qualification) +
    _foreign_reference (multipliers + daily_cap) + compute_available_for_burn().
    """
    logger.info(f"[Redeem] available_discount stub: account={account_id[:8]}, requested={requested_amount_rub}")
    # TODO Ф2: реализация
    return {
        "copilka_pts": 0,
        "ceiling_pts": 0,
        "available_pts": 0,
        "discount_rub": 0,
        "qualification": "ученик",
        "payable_rub": requested_amount_rub,
    }


async def reserve_burn(
    account_id: str,
    payment_id: str,
    points_amount: int,
    payment_source: str,
    purpose: str,
    qualification_snapshot: str,
    daily_cap_snapshot: int,
) -> bool:
    """Резерв баллов перед оплатой (status='reserved').

    Args:
        account_id: Ory UUID
        payment_id: ЮКасса payment.id ИЛИ TG Stars charge_id (idempotency)
        points_amount: сколько баллов резервируется (positive)
        payment_source: 'yookassa' / 'tg_stars' / 'stripe' / 'manual' / 'zero_payment'
        purpose: 'SEMINAR' / 'SUBSCRIPTION' / 'EVENT'
        qualification_snapshot: степень МИМ на момент резерва
        daily_cap_snapshot: daily_cap на момент резерва

    Returns:
        True — резерв создан, False — payment_id уже использован (idempotent).

    SQL (TODO Ф2):
        BEGIN;
        -- Race protection: SELECT FOR UPDATE на point_balances
        SELECT compute_available_for_burn(account_id) INTO available;
        IF available < points_amount THEN
            ROLLBACK; RETURN False;
        END IF;
        INSERT INTO rewards.redeemed_events (payment_id, account_id, points_amount, discount_rub,
            qualification_snapshot, daily_cap_snapshot, payment_source, purpose, status)
        VALUES (..., 'reserved')
        ON CONFLICT (payment_id) DO NOTHING
        RETURNING payment_id;
        COMMIT;
    """
    logger.info(f"[Redeem] reserve_burn stub: account={account_id[:8]}, payment_id={payment_id}, points={points_amount}")
    # TODO Ф2: реализация
    return True


async def confirm_burn(payment_id: str) -> bool:
    """Подтверждение резерва после успешной оплаты.

    Args:
        payment_id: ID платежа (из webhook payment.succeeded)

    Returns:
        True — подтверждено, False — payment_id не найден или уже не в status='reserved'.

    SQL (TODO Ф2):
        BEGIN;
        UPDATE rewards.redeemed_events
        SET status='confirmed', confirmed_at=now()
        WHERE payment_id = $1 AND status = 'reserved'
        RETURNING account_id, points_amount;

        -- Emit event для projection-worker
        IF FOUND THEN
            INSERT INTO learning.domain_event (event_type, payload, ingested_at, source)
            VALUES ('points_redeemed',
                    jsonb_build_object('account_id', account_id, 'points_amount', points_amount, 'payment_id', payment_id),
                    now(), 'aist_bot_redeem');
        END IF;
        COMMIT;
    """
    logger.info(f"[Redeem] confirm_burn stub: payment_id={payment_id}")
    # TODO Ф2: реализация
    return True


async def rollback_burn(payment_id: str, reason: str = "manual") -> bool:
    """Откат резерва.

    Args:
        payment_id: ID платежа
        reason: 'payment_canceled' / 'timeout_30min' / 'manual'

    Returns:
        True — откачено, False — не найдено или не в status='reserved'.

    SQL (TODO Ф2): аналогично confirm_burn, но status='rolled_back', emit reverse event.
    """
    logger.info(f"[Redeem] rollback_burn stub: payment_id={payment_id}, reason={reason}")
    # TODO Ф2: реализация
    return True


async def rollback_expired_reservations() -> int:
    """Cron-задача: откатывает резервы старше RESERVATION_TIMEOUT_MIN минут.

    Returns:
        Количество откаченных резервов.

    SQL (TODO Ф2):
        UPDATE rewards.redeemed_events
        SET status='rolled_back', rolled_back_at=now(), rollback_reason='timeout_30min'
        WHERE status='reserved'
          AND reserved_at < now() - interval '30 min'
        RETURNING payment_id, account_id, points_amount;
        -- Для каждой откаченной — emit reverse event в learning.domain_event.
    """
    logger.info(f"[Redeem] rollback_expired_reservations stub (timeout {RESERVATION_TIMEOUT_MIN} min)")
    # TODO Ф2: реализация + интеграция в core/scheduler.py
    return 0


async def get_redeem_history(account_id: str, limit: int = 10) -> list[dict]:
    """История списаний пилота для UI /points history.

    Returns: список dict'ов с полями payment_id, points_amount, discount_rub,
    purpose, status, reserved_at, confirmed_at.
    """
    logger.info(f"[Redeem] get_redeem_history stub: account={account_id[:8]}, limit={limit}")
    # TODO Ф2: SELECT FROM redeemed_events WHERE account_id ORDER BY reserved_at DESC LIMIT
    return []
