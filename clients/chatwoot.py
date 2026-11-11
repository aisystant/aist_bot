"""
Chatwoot Public API client — API Channel proxy.
see DP.SC.150, DP.ROLE.055
"""

import hashlib
import hmac
import logging
import os
import time

import aiohttp

logger = logging.getLogger(__name__)

CHATWOOT_URL = os.getenv("CHATWOOT_URL", "").rstrip("/")
CHATWOOT_INBOX_IDENTIFIER = os.getenv("CHATWOOT_INBOX_IDENTIFIER", "")
CHATWOOT_WEBHOOK_SECRET = os.getenv("CHATWOOT_WEBHOOK_SECRET", "")
CHATWOOT_API_TOKEN = os.getenv("CHATWOOT_API_TOKEN", "")
CHATWOOT_ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID", "")

_BASE = "{url}/public/api/v1/inboxes/{inbox}"


def _base() -> str:
    return _BASE.format(url=CHATWOOT_URL, inbox=CHATWOOT_INBOX_IDENTIFIER)


async def get_or_create_contact(chat_id: int, name: str) -> dict | None:
    """Create/find contact by identifier=tg_{chat_id}. Returns contact with source_id field."""
    url = f"{_base()}/contacts"
    payload = {"name": name or f"tg_{chat_id}", "identifier": f"tg_{chat_id}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            logger.error("[Chatwoot] create_contact %s: %s", resp.status, await resp.text())
            return None


async def create_conversation(source_id: str) -> dict | None:
    """Create a new conversation for the contact identified by source_id (UUID from contact response)."""
    url = f"{_base()}/contacts/{source_id}/conversations"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={}) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            logger.error("[Chatwoot] create_conversation %s: %s", resp.status, await resp.text())
            return None


async def send_message(source_id: str, conversation_id: int, content: str) -> bool:
    """Send message to conversation as the contact (source_id = UUID from contact response)."""
    url = f"{_base()}/contacts/{source_id}/conversations/{conversation_id}/messages"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={"content": content, "message_type": "outgoing"}) as resp:
            if resp.status in (200, 201):
                return True
            logger.error("[Chatwoot] send_message %s: %s", resp.status, await resp.text())
            return False


def verify_signature(payload: bytes, signature: str, timestamp: str) -> bool:
    """Verify Chatwoot's signed timestamp and raw request body.

    Fail-closed: a missing secret or a missing header rejects the request,
    otherwise anyone who knows the webhook URL could message users as support.
    """
    if not CHATWOOT_WEBHOOK_SECRET:
        logger.error("[Chatwoot] CHATWOOT_WEBHOOK_SECRET not configured — rejecting webhook")
        return False
    if not signature or not timestamp:
        return False
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - signed_at) > 300:
        return False
    signed_body = timestamp.encode() + b"." + payload
    expected = "sha256=" + hmac.new(
        CHATWOOT_WEBHOOK_SECRET.encode(), signed_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def public_agent_reply_text(messages: list, message_id: int) -> str | None:
    """Return the text of message_id if Chatwoot stores it as a public agent reply."""
    for message in messages:
        if not isinstance(message, dict) or message.get("id") != message_id:
            continue
        sender = message.get("sender") or {}
        if (
            message.get("message_type") in (1, "outgoing")
            and message.get("private") is False
            and isinstance(sender, dict)
            and sender.get("type") == "user"
        ):
            return (message.get("content") or "").strip() or None
        return None
    return None


async def fetch_public_agent_reply(conversation_id: int, message_id: int) -> str | None:
    """Read the message back from Chatwoot and return its text only if it is a public agent reply.

    Used for unsigned webhooks: the text forwarded to the user comes from Chatwoot
    itself, so a forged request cannot inject content.
    """
    if not (CHATWOOT_URL and CHATWOOT_API_TOKEN and CHATWOOT_ACCOUNT_ID):
        logger.error("[Chatwoot] API verification not configured (URL, API token, account id)")
        return None
    url = (
        f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}"
        f"/conversations/{int(conversation_id)}/messages"
    )
    params = {"before": int(message_id) + 1}
    headers = {"api_access_token": CHATWOOT_API_TOKEN}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("[Chatwoot] fetch messages conv=%s: HTTP %s", conversation_id, resp.status)
                    return None
                data = await resp.json()
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning("[Chatwoot] fetch messages conv=%s failed: %s", conversation_id, e)
        return None
    return public_agent_reply_text(data.get("payload") or [], int(message_id))
