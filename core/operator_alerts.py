"""Bounded operator alerts when the database-backed delivery queue cannot work.

Only these operational incidents may use this transport (WP-562/WP-567).
The recipient is fixed; callers cannot send arbitrary user notifications.
Delivery is best effort: an ambiguous timeout may be followed by a duplicate.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from config import DEVELOPER_CHAT_ID

logger = logging.getLogger(__name__)
SEND_TIMEOUT_SECONDS = 5


async def _send_operator_alert(bot: Bot, text: str) -> bool:
    """Return true only after Telegram acknowledges; leave the caller's bot open."""
    if not DEVELOPER_CHAT_ID:
        logger.error("[OperatorAlert] Developer recipient is not configured")
        return False
    try:
        await asyncio.wait_for(
            bot.send_message(
                DEVELOPER_CHAT_ID,
                text,
                parse_mode=None,
                protect_content=True,
                request_timeout=SEND_TIMEOUT_SECONDS,
            ),
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - A failed alert must not stop recovery.
        # Exception text can contain credentials or a copy of the payment data.
        logger.error("[OperatorAlert] Delivery failed (%s)", type(exc).__name__)
        return False
    logger.info("[OperatorAlert] Delivery acknowledged")
    return True


async def alert_recurring_donation_failure(
    bot: Bot, *, chat_id: int, charge_id: str
) -> bool:
    return await _send_operator_alert(
        bot,
        "🔴 Recurring donation НЕ сохранён: "
        f"chat_id={chat_id}, charge_id={charge_id}. "
        "Нужно проверить платёж и восстановить запись.",
    )


async def alert_stars_subscription_failure(
    bot: Bot, *, chat_id: int, charge_id: str, stars_amount: int, tariff_key: str
) -> bool:
    return await _send_operator_alert(
        bot,
        "🔴 Stars-подписка НЕ сохранена (3 попытки): "
        f"chat_id={chat_id}, charge_id={charge_id}, "
        f"amount={stars_amount} XTR, tariff={tariff_key}. "
        "Нужно проверить платёж и восстановить подписку и события.",
    )


async def alert_event_outbox_stuck(bot: Bot, *, stuck_count: int) -> bool:
    return await _send_operator_alert(
        bot,
        f"⚠️ event_outbox: {stuck_count} событий "
        "(payment_received/subscription_granted) недоставлены >10 мин — "
        "дренаж не работает",
    )
