"""WP-392 Ф3.1: Hermes-роутер.

Префикс «Гермес»/«hermes» ВСЕГДА адресует внешний Hermes-рантайм (Nous Research)
через gateway-mcp `hermes_chat` (DP.SC.167) для tier ≥ T3.

Для tier < T3 префикс «Гермес» маршрутизирует к Проводнику (DP.SC.169) —
онбординг-помощнику на Haiku с узким scope: тиры T1→T4 и следующий шаг.

Имя «Гермес»/«Hermes» зарезервировано за продуктом Nous Research. Наши собственные
артефакты (Claude-сессии, Session Memory Injector и т.д.) так НЕ называются.
"""

import logging
import re

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from config.settings import CLAUDE_MODEL_HAIKU
from db.queries import get_intern
from helpers.typing_indicator import keep_typing

logger = logging.getLogger(__name__)

hermes_router = Router(name="hermes")

_HERMES_PREFIXES = ("гермес", "hermes")
_TIER_REQUIRED = 3
_UNAVAILABLE_TIER_MSG = "Функция недоступна на твоём тире"
_UNAVAILABLE_RUNTIME_MSG = "Hermes временно недоступен. Попробуй позже."
_CONDUCTOR_UNAVAILABLE_MSG = "Проводник временно недоступен. Напиши /setup — там весь путь."
_CONDUCTOR_MAX_TOKENS = 500

_TIER_LABELS = {
    0: "T0 (анонимный)",
    1: "T1 (аккаунт подключён, без подписки)",
    2: "T2 (подписка активна)",
}


def _conductor_system_prompt(tier_num: int) -> str:
    tier_label = _TIER_LABELS.get(tier_num, f"T{tier_num}")
    return (
        "Ты Проводник — помощник по подключению к платформе Aisystant.\n\n"
        f"Текущий уровень пользователя: {tier_label}\n\n"
        "Путь оснащения: T0 (без аккаунта) → T1 (аккаунт подключён) → "
        "T2 (подписка «Инженерия интеллекта») → T3 (браузерная среда IWE) → "
        "T4 (полное окружение с VS Code).\n\n"
        "СТРОГИЙ SCOPE — отвечай ТОЛЬКО на вопросы:\n"
        "1. Что означает каждый тир и что он даёт\n"
        "2. Как перейти на следующий тир (какие действия нужны)\n"
        "3. Какие команды и функции доступны на текущем тире\n"
        "4. Общий обзор платформы: марафон, диагностика, личная база знаний\n\n"
        "Если вопрос вне scope: ответь «Я помогаю с подключением к платформе. "
        "Для других вопросов — попробуй после подписки T3, где откроется Гермес.\"\n\n"
        "Отвечай по-русски, кратко (3-5 предложений). Будь дружелюбным и конкретным."
    )


def _is_hermes_message(message: Message) -> bool:
    """Текстовое сообщение с префиксом «Гермес»/«hermes» (не из канала/группы)."""
    if message.chat.type in ("channel", "group", "supergroup"):
        return False
    text = (message.text or "").lower().lstrip()
    return text.startswith(_HERMES_PREFIXES)


def _tier_num(intern: dict) -> int:
    tier_str = intern.get("tier", "T1")
    if not isinstance(tier_str, str):
        return 1
    if tier_str == "TD1":
        return 4  # developer tier → full Hermes access
    if tier_str.startswith("T") and len(tier_str) == 2 and tier_str[1].isdigit():
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

    # Не перехватывать у SM, ожидающей ответ (марафон, фиксация дайджеста и др.)
    from handlers.external_session import _sm_is_expecting_reply
    from states.feed.digest import DigestState
    if await _sm_is_expecting_reply(chat_id) or DigestState.is_waiting_fixation(chat_id):
        logger.info("[hermes] SM or feed expecting reply for chat %s — skipping", chat_id)
        raise SkipHandler

    if not intern or not intern.get("onboarding_completed"):
        await message.answer(_UNAVAILABLE_TIER_MSG)
        return

    tier = _tier_num(intern)
    logger.info(
        "[hermes] chat_id=%s tier_str=%r tier=%s required=%s",
        chat_id, intern.get("tier"), tier, _TIER_REQUIRED,
    )

    hermes_msg = re.sub(r"^(гермес|hermes)[,:\s]+", "", text, flags=re.IGNORECASE).strip() or text

    if tier < _TIER_REQUIRED:
        # WP-349 Ф33 / DP.SC.169: Проводник — онбординг-помощник на Haiku (T1/T2).
        from clients.claude import claude
        try:
            async with keep_typing(message):
                response = await claude.generate(
                    system_prompt=_conductor_system_prompt(tier),
                    user_prompt=hermes_msg,
                    max_tokens=_CONDUCTOR_MAX_TOKENS,
                    model=CLAUDE_MODEL_HAIKU,
                    allow_partial=True,
                )
        except Exception:
            logger.exception("[hermes:conductor] Haiku call failed for chat %s", chat_id)
            response = None
        await message.answer(
            f"Проводник: {response}" if response else _CONDUCTOR_UNAVAILABLE_MSG,
            parse_mode="Markdown",
        )
        return

    from clients.gateway_mcp import gateway_mcp
    try:
        async with keep_typing(message):
            response = await gateway_mcp.hermes_chat(message=hermes_msg, telegram_user_id=chat_id)
    except Exception:
        logger.exception("[hermes] hermes_chat failed for chat %s", chat_id)
        response = _UNAVAILABLE_RUNTIME_MSG

    await message.answer(response or _UNAVAILABLE_RUNTIME_MSG, parse_mode="Markdown")
