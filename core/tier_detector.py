from __future__ import annotations

"""
UI Tier detection — subscription + connections.

Source-of-truth: WP-52 + WP-79 + WP-85 + WP-406 Ф13/Ф14 (T3 semantics)

Tier model (cumulative, payment-first):
  T0 (0):  not linked to Aisystant
  T1 (1): linked, no active БР subscription
  T2: Aisystant «Инженерия интеллекта» subscription active
  T3: T2 + AI client connected (any interface: claude.ai / Claude Code / VS Code / Telegram).
      WP-406 Ф13 consensus: T3 condition is "any AI client connected", NOT "Digital Twin".
      The platform records the signal in persona traits (tier>=T3 or mcp_connected) when a
      user connects via claude.ai; the bot must honor it so it never downgrades such a user.
  T4: T3 + GitHub connected
  T5: platform admin (DEVELOPER_CHAT_ID)

Tier drops to T1 if БР subscription expires.
Tier drops to T0 if aisystant link removed (unlikely).

TG Stars = donations only, do NOT affect tier (WP-85 decision 2026-03-12).

Tier transitions logged to tier_events table (analytics).
Downgrade T2+→T1 triggers user notification.
"""

import asyncio
import os
import logging
import time
from datetime import datetime

from core.tier_config import UITier

logger = logging.getLogger(__name__)

# In-memory tier cache: chat_id → last known tier (for transition tracking).
# Resets on deploy — intentional: avoids false transition logs on startup.
_tier_cache: dict[int, int] = {}

# TTL timestamps: chat_id → monotonic time of last full detection.
# Avoids repeated Aisystant HTTP calls on every command within the TTL window.
_tier_cache_ts: dict[int, float] = {}
_TIER_CACHE_TTL = 300  # seconds (5 min — tier changes are rare; reduces Aisystant HTTP calls)

# DEVELOPER_CHAT_ID read once at module level to avoid os.getenv on every call
_DEV_CHAT_ID: str | None = os.getenv("DEVELOPER_CHAT_ID")

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
    # T5: Platform admin (always, regardless of subscription).
    # Also persist T4 to persona so the gateway JWT-path (which only knows T0-T4)
    # grants full access. Without this write ory_identity.traits.tier stays null,
    # the DB check in gateway falls back to T1, and T3+ tools are blocked.
    # Note: if DISABLE_BOT_TIER_SYNC=true the write is silently skipped by
    # update_user_tier — in that case re-enable the flag or set persona tier manually.
    if _DEV_CHAT_ID and str(chat_id) == _DEV_CHAT_ID:
        await _persist_tier(chat_id, UITier.T4_CREATION)
        _tier_cache[chat_id] = UITier.T5_ADMIN
        _tier_cache_ts[chat_id] = time.monotonic()
        return UITier.T5_ADMIN

    # TTL cache: return cached tier if detected recently (avoids Aisystant HTTP on every command)
    now = time.monotonic()
    if chat_id in _tier_cache and now - _tier_cache_ts.get(chat_id, 0) < _TIER_CACHE_TTL:
        return _tier_cache[chat_id]

    aisystant_id = await _get_aisystant_id(chat_id)

    if not aisystant_id:
        new_tier = UITier.T0
    else:
        # Parallel: subscription check (Aisystant HTTP) + github (secrets pool) + AI-client.
        # Все три нужны только для T1+; запускаем параллельно после подтверждения aisystant_id.
        has_sub, is_github, is_ai_client = await asyncio.gather(
            _has_active_subscription(chat_id, aisystant_id),
            _is_github_connected(chat_id),
            _is_ai_client_connected(chat_id),
        )
        # Subscription first — its expiry always downgrades (no traits can override it).
        if not has_sub:
            new_tier = UITier.T1
        # DRIFT-3 fix (WP-406 Ф14): T4 requires T3 — GitHub alone never grants T4.
        elif is_github and is_ai_client:
            new_tier = UITier.T4_CREATION
        # DRIFT-1 fix (WP-406 Ф14): T3 = any AI client connected (Telegram OAuth OR
        # claude.ai/Claude Code signal recorded in persona traits).
        elif is_ai_client:
            new_tier = UITier.T3_PERSONALIZATION
        else:
            new_tier = UITier.T2_LEARNING

    # Track tier transition (fire-and-forget)
    prev_tier = _tier_cache.get(chat_id)
    if prev_tier is not None and prev_tier != new_tier:
        reason = _infer_reason(prev_tier, new_tier)
        asyncio.create_task(_log_tier_transition(chat_id, prev_tier, new_tier, reason))
        asyncio.create_task(_persist_tier(chat_id, new_tier))
        if new_tier < prev_tier:
            asyncio.create_task(_notify_downgrade(chat_id, prev_tier, new_tier))
    elif prev_tier is None:
        # First detection after deploy — persist current tier
        asyncio.create_task(_persist_tier(chat_id, new_tier))
    _tier_cache[chat_id] = new_tier
    _tier_cache_ts[chat_id] = now

    return new_tier


async def _get_aisystant_id(chat_id: int) -> str | None:
    """Get aisystant_id from DB (WP-79)."""
    try:
        from db.queries.aisystant import get_aisystant_id
        return await get_aisystant_id(chat_id)
    except Exception:
        return None


async def _get_ory_id(chat_id: int) -> str | None:
    """Get the user's Ory account UUID by chat_id.

    This UUID equals subscription.contract.account_id — the join key for the
    contract fallback. NOT the same as aisystant_id (a small Aisystant suser_id).
    """
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ory_id FROM public.users WHERE telegram_id = $1",
                chat_id,
            )
            return str(row["ory_id"]) if row and row.get("ory_id") else None
    except Exception as e:
        logger.warning(f"[Tier] Failed to resolve ory_id for {chat_id}: {e}")
        return None


async def _check_contract_subscription(chat_id: int) -> bool:
    """Check if user has an active subscription in subscription.contract (WP-7 TIR3).

    Many subscriptions live only in the contract table (legacy LMS migration);
    Aisystant's API does not know about them. The join key is the Ory account UUID
    (contract.account_id == public.users.ory_id), NOT aisystant_id.
    """
    db_url = os.getenv("SUBSCRIPTION_DATABASE_URL")
    if not db_url:
        return False

    ory_id = await _get_ory_id(chat_id)
    if not ory_id:
        return False

    try:
        import asyncpg
        conn = await asyncpg.connect(db_url)
        try:
            return bool(await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM contract
                    WHERE account_id = $1::uuid
                      AND status = 'active'
                      AND valid_to > NOW()
                )
                """,
                ory_id,
            ))
        finally:
            await conn.close()
    except Exception as e:
        logger.warning(f"[Tier] Contract subscription check failed for {chat_id}: {e}")
        return False


async def _has_active_subscription(chat_id: int, aisystant_id: str) -> bool:
    """Check if user has active Aisystant БР subscription.

    WP-85, WP-210 Ф2a: только оплаченная «Инженерия интеллекта».
    Триал убран — единственный источник T2+ права = активная БР.
    TG Stars donations do NOT affect this check.

    Contract table is a first-class source (WP-7 TIR3): many subscriptions live only
    in subscription.contract (legacy LMS migration) and Aisystant's API does not know
    about them. So when Aisystant returns False — not only on exception — fall through
    to the contract table. This is why T3/T4 users were seeing T1.
    """
    aisystant_says_active = False
    try:
        from clients.aisystant import aisystant
        aisystant_says_active = await aisystant.has_active_subscription(aisystant_id)
    except Exception as e:
        logger.warning(f"[Tier] Aisystant subscription check failed: {e}")

    if aisystant_says_active:
        return True

    # Aisystant said no (or errored) — check contract table as an equal source.
    return await _check_contract_subscription(chat_id)


async def _is_github_connected(chat_id: int) -> bool:
    """Check if user has GitHub OAuth connected."""
    try:
        from db.queries.github import get_github_connection
        return await get_github_connection(chat_id) is not None
    except Exception:
        return False


async def _is_ai_client_connected(chat_id: int) -> bool:
    """Check if the user has ANY AI client connected (T3 condition, WP-406 Ф13).

    Two signal sources, unioned:
      1. Telegram-side gateway OAuth (in-memory) — set when the user runs /connect_external.
      2. Persona traits signal — set by the platform gateway when the user connects via
         claude.ai / Claude Code (POST /tier/mcp-signal writes tier=T3 + mcp_connected=true).

    Source 2 is why a claude.ai-only user must not be downgraded by the bot: the platform
    already recorded the AI-client connection in persona traits.

    TEMP (WP-406 Ф14): reading persona traits makes the bot (authoritative tier computer
    per WP-392) depend on a derived store. This is a deliberate stopgap until WP-430
    consolidates tier ownership into the platform (bot becomes a pure reader).
    """
    try:
        from clients.gateway_mcp import gateway_mcp
        if gateway_mcp.is_connected(chat_id):
            return True
    except Exception:
        pass
    return await _persona_has_ai_signal(chat_id)


async def _persona_has_ai_signal(chat_id: int) -> bool:
    """Read persona traits to detect a platform-recorded AI-client connection.

    True if traits.mcp_connected is true OR traits.tier already at T3/T4 — both are set by
    the gateway mcp-signal on claude.ai/Claude Code OAuth. Fails closed (False) on any error.
    """
    try:
        ory_id = await _get_ory_id(chat_id)
        if not ory_id:
            return False
        from db.connection import get_persona_pool
        persona_pool = await get_persona_pool()
        async with persona_pool.acquire() as pconn:
            row = await pconn.fetchrow(
                """
                SELECT traits->>'mcp_connected' AS mcp_connected,
                       traits->>'tier'          AS tier
                FROM public.ory_identity
                WHERE account_id = $1::uuid
                """,
                ory_id,
            )
        if not row:
            return False
        if str(row["mcp_connected"]).lower() == "true":
            return True
        return row["tier"] in ("T3", "T4")
    except Exception as e:
        logger.warning(f"[Tier] persona AI-signal read failed for {chat_id}: {e}")
        return False


# ═══════════════════════════════════════════════════════════
# TIER PERSISTENCE (WP-85: sync public.users.tier)
# ═══════════════════════════════════════════════════════════

async def _persist_tier(chat_id: int, tier: int) -> None:
    """Persist computed tier to public.users (fire-and-forget).

    Топология тира (WP-392): бот = authoritative вычислитель → пишет
    public.users.tier (здесь, через update_user_tier) + эмитит tier_changed.
    persona.ory_identity.traits.tier пишет WP-270 worker по tier_changed
    (канонически) + временный дублёр в update_user_tier (флаг
    DISABLE_BOT_TIER_SYNC). Шлюз ЧИТАЕТ тир из persona через
    user-profile-service GET /tier (это читатель, не писатель).
    """
    try:
        from db.queries.identity import update_user_tier
        tier_name = f"T{tier}"
        await update_user_tier(chat_id, tier_name)
    except Exception as e:
        logger.warning(f"[Tier] Failed to persist tier for {chat_id}: {e}")


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
        from db.connection import get_rewards_pool
        pool = await get_rewards_pool()
        async with pool.acquire() as conn:
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
