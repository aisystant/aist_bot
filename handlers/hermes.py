"""WP-392 Ф3.1: Hermes-роутер.

Префикс «Гермес»/«hermes» ВСЕГДА адресует внешний Hermes-рантайм (Nous Research)
через gateway-mcp `hermes_chat` (DP.SC.167). Роутер регистрируется ДО external_session
и fallback — иначе активная /claude-сессия перехватывает «Гермес» и отвечает Claude'ом.

Имя «Гермес»/«Hermes» зарезервировано за продуктом Nous Research. Наши собственные
артефакты (Claude-сессии, Session Memory Injector и т.д.) так НЕ называются.
"""

import logging
import re

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from db.queries import get_intern

logger = logging.getLogger(__name__)

hermes_router = Router(name="hermes")

_HERMES_PREFIXES = ("гермес", "hermes")
_TIER_REQUIRED = 3
_UNAVAILABLE_TIER_MSG = "Функция недоступна на твоём тире"
_UNAVAILABLE_RUNTIME_MSG = "Hermes временно недоступен. Попробуй позже."


def _is_hermes_message(message: Message) -> bool:
    """Текстовое сообщение с префиксом «Гермес»/«hermes» (не из канала/группы)."""
    if message.chat.type in ("channel", "group", "supergroup"):
        return False
    text = (message.text or "").lower().lstrip()
    return text.startswith(_HERMES_PREFIXES)


def _tier_num(intern: dict) -> int:
    tier_str = intern.get("tier", "T1")
    if isinstance(tier_str, str) and tier_str.startswith("T") and len(tier_str) == 2 and tier_str[1].isdigit():
        return int(tier_str[1])
    return 1


@hermes_router.message(_is_hermes_message)
async def on_hermes(message: Message, state: FSMContext) -> None:
    """«Гермес, <текст>» → hermes_chat. Tier < T3 → отказ.

    WP-392 Ф3.1: префикс «Гермес» имеет абсолютный приоритет.
    SkipHandler ТОЛЬКО если marathon SM ждёт ответ (не ломать марафон).
    Не-онбордированным показываем отказ tier — не пропускаем в fallback.
    """
    chat_id = message.chat.id
    text = message.text or ""

    intern = await get_intern(chat_id)

    # Не перехватывать у marathon SM, ожидающей ответ пользователя.
    from handlers.external_session import _sm_is_expecting_reply
    if await _sm_is_expecting_reply(chat_id):
        logger.info("[hermes] SM expecting reply for chat %s — skipping", chat_id)
        raise SkipHandler

    if not intern or not intern.get("onboarding_completed"):
        await message.answer(_UNAVAILABLE_TIER_MSG)
        return

    tier_num = _tier_num(intern)
    logger.info("[hermes] chat_id=%s tier_str=%r tier_num=%s required=%s", chat_id, intern.get("tier"), tier_num, _TIER_REQUIRED)
    if tier_num < _TIER_REQUIRED:
        await message.answer(_UNAVAILABLE_TIER_MSG)
        return

    hermes_msg = re.sub(r"^(гермес|hermes)[,:\s]+", "", text, flags=re.IGNORECASE).strip() or text

    from clients.gateway_mcp import gateway_mcp
    try:
        from helpers.typing_indicator import keep_typing
        async with keep_typing(message):
            response = await gateway_mcp.hermes_chat(message=hermes_msg, telegram_user_id=chat_id)
    except Exception:
        logger.exception("[hermes] hermes_chat failed for chat %s", chat_id)
        response = _UNAVAILABLE_RUNTIME_MSG

    await message.answer(response or _UNAVAILABLE_RUNTIME_MSG)
