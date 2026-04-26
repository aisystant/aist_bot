"""
Запросы для работы с подписками.

Две таблицы:
- subscriptions (aist_bot БД) — Stars-донаты, внутренний учёт бота
- subscription_grants (platform БД) — реестр прав доступа к Gateway (WP-231 Ф-H)
"""

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
    """Subscription grant — single-write на event-gateway (WP-268 cut-over).

    WP-268 cut-over: legacy UPDATE/INSERT INTO subscription_grants (platform БД)
    УДАЛЕНЫ. Источник истины для grant'ов теперь — event-gateway projection.

    ⚠️ КРИТИЧЕСКАЯ ОСОБЕННОСТЬ: subscription_grants была расположена в platform
    БД, которая НЕ aist_bot legacy. Это уже была "новая БД" (Pattern 2). Cut-over
    в этом случае означает миграцию с прямого write → event-driven write.
    Это намеренное решение Tseren'а: subscription_grants gets rebuilt from
    events. Без legacy WRITE → bot не может больше синхронно гарантировать
    активный грант после оплаты — это делает projection.

    ⚠️ READ path (Gateway authorization → SELECT FROM subscription_grants)
    продолжает работать ТОЛЬКО ЕСЛИ projection догнала. Если projection
    задержалась — пользователь может получить "no active grant" сразу после
    оплаты. Mitigation: webhook YooKassa retry (idempotent через external_id).

    Идемпотентность: external_id привязан к (telegram_id, source, valid_until).
    Повторный вызов с теми же args = тот же external_id = gateway dedup.

    Args:
        telegram_id: Telegram chat_id пользователя.
        valid_until: Дата окончания подписки (naive UTC).
        source: Источник права — 'tg_stars' или 'bot_payment'.
    """
    # WP-268 cut-over: единственный writer — event-gateway. mode='upsert'
    # потому что без read-modify-write мы не знаем «extend vs create».
    # Projection в новой БД должна сама решать (LATEST valid_until WINS).
    await _emit_subscription_granted(telegram_id, source, valid_until, mode="upsert")
    logger.info(
        f"[SubscriptionGrant] Emitted (event-only): telegram_id={telegram_id}, "
        f"source={source}, valid_until={valid_until}"
    )


async def _emit_subscription_granted(
    telegram_id: int,
    source: str,
    valid_until: datetime,
    mode: str,
) -> None:
    """WP-268 cut-over: subscription_granted event (single writer)."""
    now = datetime.utcnow()
    valid_until_iso = valid_until.isoformat() if hasattr(valid_until, "isoformat") else str(valid_until)
    await post_event(
        source="aist-bot",
        external_id=f"sub-granted-{telegram_id}-{source}-{valid_until_iso}",
        event_type="subscription_granted",
        schema_version="v1",
        occurred_at=now,
        account_id=None,  # ory_id не доступен (bot-scope = только telegram_id)
        payload={
            # ⚠️ telegram_id остаётся в payload (бизнес-нужда: subscription_grants
            # projection должна знать кому grant). Это документированное
            # отклонение от PII-инварианта (DP.ARCH.004 §2: payment data tier).
            "telegram_id": telegram_id,
            "product": "br",
            "source": source,
            "valid_until": valid_until_iso,
            "mode": mode,  # "upsert" — projection решает create vs extend
        },
    )
