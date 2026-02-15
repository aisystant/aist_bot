"""
Запросы для работы с подписками (таблица subscriptions).
"""

from datetime import datetime
from typing import Optional

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def get_active_subscription(chat_id: int) -> Optional[dict]:
    """Получить активную подписку пользователя.

    Returns:
        dict с данными подписки или None.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT id, chat_id, telegram_payment_charge_id,
                      status, stars_amount, started_at, expires_at,
                      cancelled_at, is_first_recurring, created_at
               FROM subscriptions
               WHERE chat_id = $1
                 AND status = 'active'
                 AND expires_at > NOW()
               ORDER BY expires_at DESC
               LIMIT 1''',
            chat_id,
        )
        if row:
            return dict(row)
        return None


async def is_subscribed(chat_id: int) -> bool:
    """Проверить, есть ли у пользователя активная подписка."""
    sub = await get_active_subscription(chat_id)
    return sub is not None


async def save_subscription(
    chat_id: int,
    charge_id: str,
    stars_amount: int,
    expires_at: datetime,
    is_first: bool = False,
) -> int:
    """Сохранить новую подписку (или продление).

    Returns:
        ID записи.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            '''INSERT INTO subscriptions
               (chat_id, telegram_payment_charge_id, status,
                stars_amount, expires_at, is_first_recurring)
               VALUES ($1, $2, 'active', $3, $4, $5)
               RETURNING id''',
            chat_id, charge_id, stars_amount, expires_at, is_first,
        )
        logger.info(
            f"[Subscription] Saved: chat_id={chat_id}, "
            f"amount={stars_amount} Stars, expires={expires_at}"
        )
        return row_id


async def cancel_subscription(chat_id: int, charge_id: str) -> None:
    """Отменить подписку (статус → cancelled)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''UPDATE subscriptions
               SET status = 'cancelled', cancelled_at = NOW()
               WHERE chat_id = $1
                 AND telegram_payment_charge_id = $2
                 AND status = 'active' ''',
            chat_id, charge_id,
        )
        logger.info(f"[Subscription] Cancelled: chat_id={chat_id}")


async def get_subscription_history(chat_id: int, limit: int = 10) -> list[dict]:
    """История подписок пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT id, status, stars_amount, started_at, expires_at,
                      cancelled_at, created_at
               FROM subscriptions
               WHERE chat_id = $1
               ORDER BY created_at DESC
               LIMIT $2''',
            chat_id, limit,
        )
        return [dict(r) for r in rows]


async def get_trial_expiring_users(days_ahead: int = 0) -> list[int]:
    """Найти пользователей, чей триал истекает через N дней.

    Args:
        days_ahead: 0 = сегодня, 1 = завтра.

    Returns:
        Список chat_id.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        from config.settings import FREE_TRIAL_DAYS
        rows = await conn.fetch(
            '''SELECT chat_id FROM interns
               WHERE onboarding_completed = TRUE
                 AND trial_started_at IS NOT NULL
                 AND trial_started_at + INTERVAL '1 day' * $1 <= NOW()
                 AND trial_started_at + INTERVAL '1 day' * ($1 + 1) > NOW()
                 AND chat_id NOT IN (
                     SELECT chat_id FROM subscriptions
                     WHERE status = 'active' AND expires_at > NOW()
                 )''',
            FREE_TRIAL_DAYS - days_ahead,
        )
        return [r['chat_id'] for r in rows]
