"""
Запросы для работы с подписками.

Две таблицы (обе в platform БД):
- subscriptions — Stars-донаты, внутренний учёт бота
- subscription_grants — реестр прав доступа к Gateway (WP-231 Ф-H)
"""

import asyncio
from datetime import datetime
from typing import Optional

from config import get_logger
from db.connection import get_pool
from helpers.dual_write import post_event

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
                      status, stars_amount, created_at AS started_at,
                      expires_at, created_at
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
                stars_amount, expires_at)
               VALUES ($1, $2, 'active', $3, $4)
               RETURNING id''',
            chat_id, charge_id, stars_amount, expires_at,
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
               SET status = 'cancelled'
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
            '''SELECT id, status, stars_amount, created_at AS started_at,
                      expires_at, created_at
               FROM subscriptions
               WHERE chat_id = $1
               ORDER BY created_at DESC
               LIMIT $2''',
            chat_id, limit,
        )
        return [dict(r) for r in rows]


async def upsert_subscription_grant(
    telegram_id: int,
    valid_until: datetime,
    source: str,
) -> None:
    """Записать/продлить право доступа в реестр подписок (platform БД).

    WP-231 Ф-H: вызывается после успешной оплаты через бота (Stars, ЮКасса),
    когда email неизвестен — только telegram_id.

    Логика:
    - Ищем активный грант по telegram_id с source IN ('tg_stars', 'bot_payment').
    - Если найден → продлить valid_until до наибольшего значения.
    - Если нет → INSERT новой строки.
    - ory_id не заполняется — появится позже через Вариант A (Gateway OAuth) или sync.

    Не используем ON CONFLICT — в subscription_grants нет UNIQUE на telegram_id
    (у одного telegram_id может быть несколько грантов из разных источников: lms_sync, manual).

    Args:
        telegram_id: Telegram chat_id пользователя.
        valid_until: Дата окончания подписки (naive UTC).
        source: Источник права — 'tg_stars' или 'bot_payment'.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        updated = await conn.execute(
            '''
            UPDATE subscription_grants
            SET valid_until = GREATEST(valid_until, $1)
            WHERE telegram_id = $2
              AND source IN ('tg_stars', 'bot_payment')
              AND revoked_at IS NULL
            ''',
            valid_until, telegram_id,
        )
        # asyncpg returns 'UPDATE N' — если N > 0, строка обновлена
        if updated and updated.split()[-1] != '0':
            logger.info(
                f"[SubscriptionGrant] Extended: telegram_id={telegram_id}, "
                f"source={source}, valid_until={valid_until}"
            )
            _fire_subscription_granted(telegram_id, source, valid_until, mode="extended")
            return

        await conn.execute(
            '''
            INSERT INTO subscription_grants
                (telegram_id, product, source, valid_from, valid_until, granted_by)
            VALUES
                ($1, 'br', $2, NOW(), $3, 'system')
            ''',
            telegram_id, source, valid_until,
        )
        logger.info(
            f"[SubscriptionGrant] Created: telegram_id={telegram_id}, "
            f"source={source}, valid_until={valid_until}"
        )
        _fire_subscription_granted(telegram_id, source, valid_until, mode="created")


def _fire_subscription_granted(
    telegram_id: int,
    source: str,
    valid_until: datetime,
    mode: str,
) -> None:
    """WP-268 Phase 2 dual-write: subscription_granted event."""
    try:
        now = datetime.utcnow()
        # external_id идемпотентен по комбинации (telegram_id, source, valid_until ISO)
        # Если оплата идемпотентно вызывает upsert — событие тоже идемпотентно.
        valid_until_iso = valid_until.isoformat() if hasattr(valid_until, "isoformat") else str(valid_until)
        asyncio.create_task(post_event(
            source="aist-bot",
            external_id=f"sub-granted-{telegram_id}-{source}-{valid_until_iso}",
            event_type="subscription_granted",
            schema_version="v1",
            occurred_at=now,
            account_id=None,  # ory_id здесь не доступен (bot-scope только telegram_id)
            # PII fix (26 апр): telegram_id убран из payload (FORBIDDEN_FIELDS gateway).
            # Для projection-worker'а (WP-270) — извлекается из external_id префикса.
            payload={
                "product": "br",
                "source": source,
                "valid_until": valid_until_iso,
                "mode": mode,  # "created" | "extended"
            },
        ))
    except Exception as exc:
        logger.warning(f"[dual-write] subscription_granted fire failed: {exc}")
