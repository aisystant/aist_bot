"""
UI Tier detection — subscription + trial + connections.

Source-of-truth: WP-52 + WP-79 + WP-85

Tier model (cumulative, payment-first):
  T0 (0):  not linked to Aisystant
  T1 (1): linked, no БР subscription AND trial expired
  T2: Aisystant «Бесконечное развитие» subscription active OR 30-day trial from /start
  T3: T2 + Digital Twin connected
  T4: T3 + GitHub connected
  T5: platform admin (DEVELOPER_CHAT_ID)

Tier drops to T1 if БР subscription expires AND trial expired.
Tier drops to T0 if aisystant link removed (unlikely).

TG Stars = donations only, do NOT affect tier (WP-85 decision 2026-03-12).

Tier transitions logged to tier_events table (analytics).
Downgrade T2+→T1 triggers user notification.
"""

import asyncio
import os
import logging
from datetime import datetime

from core.tier_config import UITier

logger = logging.getLogger(__name__)

# In-memory tier cache: chat_id → last known tier.
# Resets on deploy — intentional: avoids false transition logs on startup.
_tier_cache: dict[int, int] = {}

_TIER_NAMES = {
    UITier.T0: "New",
    UITier.T1: "Start",
    UITier.T2_LEARNING: "Learning",
    UITier.T3_PERSONALIZATION: "Personalization",
    UITier.T4_CREATION: "Creation",
    UITier.T5_ADMIN: "Admin",
}


async def detect_ui_tier(chat_id: int) -> int:
    """Detect UI tier based on Aisystant link + БР subscription + connections.

    Also tracks tier transitions: logs to tier_events + notifies on downgrade.

    Args:
        chat_id: Telegram user chat_id

    Returns:
        UITier constant (0-5)
    """
    # T5: Platform admin (always, regardless of subscription)
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    if dev_chat_id and str(chat_id) == dev_chat_id:
        return UITier.T5_ADMIN

    # WP-79: Check Aisystant link first
    aisystant_id = await _get_aisystant_id(chat_id)

    if not aisystant_id:
        new_tier = UITier.T0
    elif not await _has_active_subscription(chat_id, aisystant_id):
        new_tier = UITier.T1
    elif await _is_github_connected(chat_id):
        new_tier = UITier.T4_CREATION
    elif await _is_dt_connected(chat_id):
        new_tier = UITier.T3_PERSONALIZATION
    else:
        new_tier = UITier.T2_LEARNING

    # Track tier transition (fire-and-forget)
    prev_tier = _tier_cache.get(chat_id)
    if prev_tier is not None and prev_tier != new_tier:
        reason = _infer_reason(prev_tier, new_tier)
        asyncio.create_task(_log_tier_transition(chat_id, prev_tier, new_tier, reason))
        if new_tier < prev_tier:
            asyncio.create_task(_notify_downgrade(chat_id, prev_tier, new_tier))
    _tier_cache[chat_id] = new_tier

    return new_tier


async def _get_aisystant_id(chat_id: int) -> str | None:
    """Get aisystant_id from DB (WP-79)."""
    try:
        from db.queries.aisystant import get_aisystant_id
        return await get_aisystant_id(chat_id)
    except Exception:
        return None


async def _has_active_subscription(chat_id: int, aisystant_id: str) -> bool:
    """Check if user has active Aisystant БР subscription OR is in trial.

    WP-85: Aisystant «Бесконечное развитие» OR 30-day trial from /start.
    TG Stars donations do NOT affect this check.
    """
    # Primary: Aisystant БР
    try:
        from clients.aisystant import aisystant
        if await aisystant.has_active_subscription(aisystant_id):
            return True
    except Exception as e:
        logger.warning(f"[Tier] Aisystant subscription check failed: {e}")

    # Fallback: 30-day trial from /start
    from core.access import access_layer
    return await access_layer.is_in_trial(chat_id)


async def _is_github_connected(chat_id: int) -> bool:
    """Check if user has GitHub OAuth connected."""
    try:
        from db.queries.github import get_github_connection
        return await get_github_connection(chat_id) is not None
    except Exception:
        return False


async def _is_dt_connected(chat_id: int) -> bool:
    """Check if Digital Twin is connected."""
    try:
        from db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT dt_connected_at FROM public.users WHERE telegram_id = $1', chat_id,
            )
            return row is not None and row['dt_connected_at'] is not None
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════
# TIER TRANSITION TRACKING (WP-52 analytics)
# ═══════════════════════════════════════════════════════════

def _infer_reason(from_tier: int, to_tier: int) -> str:
    """Infer the reason for a tier transition."""
    if to_tier == UITier.T0:
        return "aisystant_unlinked"
    if to_tier == UITier.T1 and from_tier >= UITier.T2_LEARNING:
        return "subscription_expired"
    if to_tier == UITier.T1 and from_tier == UITier.T0:
        return "aisystant_linked"
    if to_tier == UITier.T2_LEARNING and from_tier <= UITier.T1:
        return "subscription_activated"
    if to_tier == UITier.T3_PERSONALIZATION:
        return "dt_connected"
    if to_tier == UITier.T4_CREATION:
        return "github_connected"
    if to_tier < from_tier:
        return "downgrade"
    return "upgrade"


async def _log_tier_transition(chat_id: int, from_tier: int, to_tier: int, reason: str) -> None:
    """Log tier transition to tier_events table (fire-and-forget)."""
    try:
        from db.connection import acquire
        async with await acquire() as conn:
            await conn.execute(
                """INSERT INTO tier_events (chat_id, from_tier, to_tier, reason, created_at)
                   VALUES ($1, $2, $3, $4, $5)""",
                chat_id, from_tier, to_tier, reason, datetime.utcnow(),
            )
        direction = "upgrade" if to_tier > from_tier else "downgrade"
        logger.info(
            f"[Tier] {direction}: user {chat_id} "
            f"T{from_tier}({_TIER_NAMES.get(from_tier, '?')}) → "
            f"T{to_tier}({_TIER_NAMES.get(to_tier, '?')}) [{reason}]"
        )
    except Exception as e:
        logger.error(f"[Tier] Failed to log transition: {e}")


async def _notify_downgrade(chat_id: int, from_tier: int, to_tier: int) -> None:
    """Notify user when their tier drops (e.g. subscription expired)."""
    try:
        from aiogram import Bot
        bot_token = os.getenv("BOT_TOKEN")
        if not bot_token:
            return

        # Only notify on meaningful downgrades (T2+→T1 = subscription expired)
        if to_tier > UITier.T1:
            return

        from_name = _TIER_NAMES.get(from_tier, f"T{from_tier}")
        bot = Bot(token=bot_token)
        try:
            await bot.send_message(
                chat_id,
                f"Подписка истекла. Часть сервисов ограничена.\n"
                f"Напиши /start чтобы увидеть доступные функции.",
            )
        finally:
            await bot.session.close()
    except Exception as e:
        logger.error(f"[Tier] Failed to send downgrade notification: {e}")
