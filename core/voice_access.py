"""Who may use voice notes and Siri notes (WP-384).

Off for everyone except the developer (DEVELOPER_CHAT_ID) and the chats listed in
VOICE_ALLOWED_CHAT_IDS, so promoting `pilot` to production does not switch voice
on for all users before the release decision ("*" opens it to everyone).
"""

from __future__ import annotations

import logging

from config.settings import DEVELOPER_CHAT_ID, VOICE_ALLOWED_CHAT_IDS

logger = logging.getLogger(__name__)

EVERYONE = "*"


def parse_allowed(raw: str) -> frozenset[int] | None:
    """Allowed chat ids from the setting; None means everyone. Bad items are skipped."""
    items = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if EVERYONE in items:
        return None
    allowed = set()
    for item in items:
        try:
            allowed.add(int(item))
        except ValueError:
            logger.warning("[voice_access] VOICE_ALLOWED_CHAT_IDS: skipped non-numeric item")
    return frozenset(allowed)


_ALLOWED = parse_allowed(VOICE_ALLOWED_CHAT_IDS)


def voice_enabled_for(chat_id: int) -> bool:
    if _ALLOWED is None:
        return True
    return chat_id in _ALLOWED or (DEVELOPER_CHAT_ID != 0 and chat_id == DEVELOPER_CHAT_ID)
