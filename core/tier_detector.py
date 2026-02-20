"""
UI Tier detection — behavioral, not payment.

Source-of-truth: WP-52 § 5.2

Tier transitions:
  T1→T2: marathon_status == 'completed'
  T2→T3: marathon completed + DT connected (dt_connected_at IS NOT NULL)
  T3→T4: has exocortex (deferred)
  T4→T5: platform owner (DEVELOPER_CHAT_ID)
"""

import os
import logging

from core.tier_config import UITier

logger = logging.getLogger(__name__)


def detect_ui_tier(user: dict) -> int:
    """Detect UI tier based on user behavior.

    Args:
        user: intern dict from get_intern()

    Returns:
        UITier constant (1-5)
    """
    chat_id = user.get('chat_id')

    # T5: Platform admin
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    if dev_chat_id and str(chat_id) == dev_chat_id:
        return UITier.T5_ADMIN

    # T3: Marathon completed + DT connected
    if _is_marathon_completed(user) and _is_dt_connected(user):
        return UITier.T3_PERSONALIZATION

    # T2: Marathon completed
    if _is_marathon_completed(user):
        return UITier.T2_LEARNING

    # T1: Default (new user, in marathon)
    return UITier.T1_START


def _is_marathon_completed(user: dict) -> bool:
    """Check if marathon is completed."""
    return user.get('marathon_status') == 'completed'


def _is_dt_connected(user: dict) -> bool:
    """Check if Digital Twin is connected via /twin."""
    return user.get('dt_connected_at') is not None
