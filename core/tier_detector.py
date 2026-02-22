"""
UI Tier detection — subscription + connections.

Source-of-truth: WP-52

Tier model (cumulative, payment-first):
  T1: free (no active subscription/trial)
  T2: active subscription or trial
  T3: T2 + Digital Twin connected
  T4: T3 + GitHub connected
  T5: platform admin (DEVELOPER_CHAT_ID)

Tier drops to T1 if subscription expires.
"""

import os
import logging

from core.tier_config import UITier

logger = logging.getLogger(__name__)


async def detect_ui_tier(chat_id: int) -> int:
    """Detect UI tier based on subscription + connections.

    Args:
        chat_id: Telegram user chat_id

    Returns:
        UITier constant (1-5)
    """
    # T5: Platform admin (always, regardless of subscription)
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    if dev_chat_id and str(chat_id) == dev_chat_id:
        return UITier.T5_ADMIN

    # Check subscription — no active sub/trial = T1
    if not await _has_active_subscription(chat_id):
        return UITier.T1_START

    # T4: subscription + GitHub connected
    if await _is_github_connected(chat_id):
        return UITier.T4_CREATION

    # T3: subscription + DT connected
    if await _is_dt_connected(chat_id):
        return UITier.T3_PERSONALIZATION

    # T2: subscription active
    return UITier.T2_LEARNING


async def _has_active_subscription(chat_id: int) -> bool:
    """Check if user has active subscription or trial."""
    from core.access import access_layer
    return await access_layer.has_access(chat_id, "feed")


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
                'SELECT dt_connected_at FROM interns WHERE chat_id = $1', chat_id,
            )
            return row is not None and row['dt_connected_at'] is not None
    except Exception:
        return False
