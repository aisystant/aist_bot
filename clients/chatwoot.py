"""
Chatwoot Public API client — API Channel proxy.
see DP.SC.150, DP.ROLE.055
"""

import hashlib
import hmac
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

CHATWOOT_URL = os.getenv("CHATWOOT_URL", "").rstrip("/")
CHATWOOT_INBOX_IDENTIFIER = os.getenv("CHATWOOT_INBOX_IDENTIFIER", "")
CHATWOOT_WEBHOOK_SECRET = os.getenv("CHATWOOT_WEBHOOK_SECRET", "")

_BASE = "{url}/public/api/v1/inboxes/{inbox}"


def _base() -> str:
    return _BASE.format(url=CHATWOOT_URL, inbox=CHATWOOT_INBOX_IDENTIFIER)


async def get_or_create_contact(chat_id: int, name: str) -> dict | None:
    """Create/find contact by identifier=tg_{chat_id}."""
    url = f"{_base()}/contacts"
    payload = {"name": name or f"tg_{chat_id}", "identifier": f"tg_{chat_id}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            logger.error("[Chatwoot] create_contact %s: %s", resp.status, await resp.text())
            return None


async def create_conversation(contact_identifier: str) -> dict | None:
    """Create a new conversation for the contact."""
    url = f"{_base()}/contacts/{contact_identifier}/conversations"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={}) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            logger.error("[Chatwoot] create_conversation %s: %s", resp.status, await resp.text())
            return None


async def send_message(contact_identifier: str, conversation_id: int, content: str) -> bool:
    """Send message to conversation as the contact."""
    url = f"{_base()}/contacts/{contact_identifier}/conversations/{conversation_id}/messages"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={"content": content, "message_type": "outgoing"}) as resp:
            if resp.status in (200, 201):
                return True
            logger.error("[Chatwoot] send_message %s: %s", resp.status, await resp.text())
            return False


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify HMAC-SHA256 from Chatwoot X-Chatwoot-Signature header."""
    if not CHATWOOT_WEBHOOK_SECRET:
        return True
    expected = hmac.new(
        CHATWOOT_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
