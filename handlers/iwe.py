"""WP-428 Ф2: IWE unified dialog entry-point.

Responds to /iwe <query> and .<query> (dot-prefix).
T3+ → gateway_mcp.hermes_chat; T<3 → Haiku fallback.
Privacy by design: blocks cp.xxx= and email patterns in response (DP.SC.183).
"""

import logging
import re

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import Message

from config.settings import CLAUDE_MODEL_HAIKU
from db.queries import get_intern
from helpers.typing_indicator import keep_typing

logger = logging.getLogger(__name__)

iwe_router = Router(name="iwe")

_UNAVAILABLE_MSG = "Сейчас недоступен. Попробуй позже."
_PRIVACY_BLOCK_MSG = "Не могу ответить на этот запрос. Попробуй переформулировать."

# True PII patterns — block the response:
_PII_BLOCK = [re.compile(r"cp\.\w+="), re.compile(r"@\w+\.\w+")]
# Identifier patterns — log only, do not block:
_PII_LOG = [re.compile(r"WP-\d+")]

_TIER_REQUIRED = 3


def _is_iwe_dot(message: Message) -> bool:
    """Text starts with '.' followed by content (not from channels/groups)."""
    if message.chat.type in ("channel", "group", "supergroup"):
        return False
    text = message.text or ""
    return text.startswith(".") and len(text) > 1


@iwe_router.message(Command("iwe"))
@iwe_router.message(_is_iwe_dot)
async def on_iwe(message: Message) -> None:
    """IWE dialog entry: /iwe <query> or .<query> → hermes_chat or Haiku."""
    chat_id = message.chat.id
    text = (message.text or "").strip()

    # Extract the user's query
    if text.startswith("/iwe"):
        query = text[4:].strip()
    else:
        query = text[1:].strip()  # remove leading "."

    if not query:
        await message.answer("Напиши вопрос: /iwe <вопрос> или . <вопрос>")
        return

    # Guard: don't intercept while SM or feed digest is expecting a reply
    from handlers.external_session import _sm_is_expecting_reply
    from states.feed.digest import FeedDigestState
    if await _sm_is_expecting_reply(chat_id) or FeedDigestState.is_waiting_fixation(chat_id):
        logger.info("[iwe] SM or feed expecting reply for chat %s — skipping", chat_id)
        raise SkipHandler

    intern = await get_intern(chat_id)
    if not intern or not intern.get("onboarding_completed"):
        await message.answer(_UNAVAILABLE_MSG)
        return

    from core.tier_detector import detect_ui_tier
    tier = await detect_ui_tier(chat_id)
    logger.info("[iwe] chat_id=%s tier=%s", chat_id, tier)

    response = None

    if tier >= _TIER_REQUIRED:
        from clients.gateway_mcp import gateway_mcp
        async with keep_typing(message):
            try:
                response = await gateway_mcp.hermes_chat(message=query, telegram_user_id=chat_id)
            except Exception:
                logger.exception("[iwe] hermes_chat failed for chat %s", chat_id)
                response = _UNAVAILABLE_MSG
    else:
        from clients.claude import claude
        async with keep_typing(message):
            try:
                response = await claude.generate(
                    system_prompt=(
                        "Ты IWE-помощник (Intellectual Work Environment). "
                        "Отвечай кратко по-русски, 3-5 предложений."
                    ),
                    user_prompt=query,
                    max_tokens=300,
                    model=CLAUDE_MODEL_HAIKU,
                    allow_partial=True,
                )
            except Exception:
                logger.exception("[iwe] haiku fallback failed for chat %s", chat_id)

    if not response:
        await message.answer(_UNAVAILABLE_MSG)
        return

    # Privacy guard: block on true PII patterns
    for pat in _PII_BLOCK:
        if pat.search(response):
            logger.error(
                "[iwe] PII block pattern '%s' in response for chat=%s",
                pat.pattern, chat_id,
            )
            await message.answer(_PRIVACY_BLOCK_MSG)
            return

    # Privacy guard: log-only for task identifiers
    for pat in _PII_LOG:
        if pat.search(response):
            logger.warning(
                "[iwe] PII log pattern '%s' in response for chat=%s",
                pat.pattern, chat_id,
            )

    await message.answer(response, parse_mode="Markdown")
