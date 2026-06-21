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
import time

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from config.settings import CLAUDE_MODEL_HAIKU
from db.queries import get_intern
from helpers.typing_indicator import keep_typing

logger = logging.getLogger(__name__)

hermes_router = Router(name="hermes")

_HERMES_PREFIXES = ("гермес", "hermes")
_HERMES_SESSION_KEY = "hermes_session_id"  # FSM key for per-user session override (WP-428 Ф7)
_TIER_REQUIRED = 3
_UNAVAILABLE_TIER_MSG = "Функция недоступна на твоём тире"
_UNAVAILABLE_RUNTIME_MSG = "Hermes временно недоступен. Попробуй позже."
_CONDUCTOR_UNAVAILABLE_MSG = "Проводник временно недоступен. Напиши /setup — там весь путь."
_CONDUCTOR_MAX_TOKENS = 500

_TIER_LABELS = {
    0: "T0 (анонимный)",
    1: "T1 (аккаунт подключён, без подписки)",
    2: "T2 (подписка активна)",
    3: "T3 (браузерная среда IWE)",
    4: "T4 (полное окружение с VS Code)",
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


# Онбординг-релевантные запросы: только на них Проводник предлагает кнопку
# «Освоиться» (вход Онбордера). На частные вопросы («какой день марафона»)
# кнопку НЕ навязываем — Гермес остаётся Q&A, а не push (консенсус 2026-06-11-20).
_ONBOARDING_QUERY_KEYWORDS = (
    "начать", "с чего", "как устроен", "ориентац", "первый шаг",
    "как тут", "что здесь", "что тут", "куда идти", "освоит", "с нуля",
)


def _is_onboarding_query(text: str) -> bool:
    """Вопрос про вход в сообщество (а не частность) — по ключевым словам."""
    low = (text or "").lower()
    return any(kw in low for kw in _ONBOARDING_QUERY_KEYWORDS)


@hermes_router.message(Command("hermes_reset"))
async def on_hermes_reset(message: Message, state: FSMContext) -> None:
    """WP-428 Ф7: reset Hermes conversation history by switching to a new session_id."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    new_sid = f"tg-{user_id}-reset-{int(time.time())}"
    await state.update_data({_HERMES_SESSION_KEY: new_sid})
    await message.answer("История Гермеса сброшена.")


@hermes_router.message(_is_hermes_message)
async def on_hermes(message: Message, state: FSMContext) -> None:
    """«Гермес, <текст>» → hermes_chat. Tier < T3 → отказ.

    WP-392 Ф3.1: префикс «Гермес» имеет абсолютный приоритет.
    SkipHandler ТОЛЬКО если marathon SM ждёт ответ (не ломать марафон).
    Не-онбордированным показываем отказ tier — не пропускаем в fallback.
    WP-428 Ф7: session_id threads conversation history through Hermes.
    """
    if message.from_user is None:  # Hermes is per-user; skip anonymous posts
        return
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text or ""

    intern = await get_intern(chat_id)

    # Не перехватывать у SM, ожидающей ответ (марафон, фиксация дайджеста и др.)
    from handlers.external_session import _sm_is_expecting_reply
    from states.feed.digest import FeedDigestState
    if await _sm_is_expecting_reply(chat_id) or FeedDigestState.is_waiting_fixation(chat_id):
        logger.info("[hermes] SM or feed expecting reply for chat %s — skipping", chat_id)
        raise SkipHandler

    if not intern or not intern.get("onboarding_completed"):
        await message.answer(_UNAVAILABLE_TIER_MSG)
        return

    # Используем detect_ui_tier для актуального тира, а не устаревшее поле из БД.
    # get_intern читает public.users.tier, которое обновляется только при переходах тира
    # через detect_ui_tier. Если тир менялся (подключили GitHub/ЦД) между деплоями,
    # DB-поле может быть stale — тогда Проводник неверно отказывает T4-пользователям.
    from core.tier_detector import detect_ui_tier
    tier = await detect_ui_tier(chat_id)
    logger.info(
        "[hermes] chat_id=%s tier=%s required=%s",
        chat_id, tier, _TIER_REQUIRED,
    )

    hermes_msg = re.sub(r"^(гермес|hermes)[,:\s]+", "", text, flags=re.IGNORECASE).strip() or text

    if tier < _TIER_REQUIRED:
        # DP.SC.169 deprecated (WP-406 Ф7) → поглощён Онбордером (DP.SC.170).
        # Гибрид (WP-406 Ф5): живой Haiku-ответ Проводника на вопрос + кнопка
        # «Освоиться» (вход Онбордера) только на онбординг-релевантные запросы.
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
        await _maybe_offer_onboarder_after_conductor(message, chat_id, hermes_msg)
        return

    state_data = await state.get_data()
    session_id = state_data.get(_HERMES_SESSION_KEY, f"tg-{user_id}")

    from clients.gateway_mcp import gateway_mcp
    try:
        async with keep_typing(message):
            response = await gateway_mcp.hermes_chat(
                message=hermes_msg,
                telegram_user_id=chat_id,
                session_id=session_id,
            )
    except Exception:
        logger.exception("[hermes] hermes_chat failed for chat %s", chat_id)
        response = _UNAVAILABLE_RUNTIME_MSG

    await message.answer(response or _UNAVAILABLE_RUNTIME_MSG, parse_mode="Markdown")


async def _maybe_offer_onboarder_after_conductor(message: Message, chat_id: int, query: str) -> None:
    """После ответа Проводника (tier<T3) предложить вход Онбордера.

    Двойной гейт: (1) вопрос про вход в сообщество (_is_onboarding_query), иначе
    Гермес остаётся Q&A без push; (2) есть открытый разрыв Х2/Х3 и не на cooldown
    (offer.should_offer). Отрисовка кнопки из общего payload — без дублирования
    с onboarding.py.
    """
    if not _is_onboarding_query(query):
        return
    from core.onboarder import offer
    try:
        if not await offer.should_offer(chat_id):
            return
    except Exception as e:
        logger.warning("[hermes] should_offer check failed for %s: %s", chat_id, e)
        return
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    payload = offer.offer_payload()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=payload["button_text"], callback_data=payload["callback_data"]),
    ]])
    await message.answer(payload["text"], reply_markup=kb)
    try:
        await offer.mark_offered(chat_id)
    except Exception as e:
        logger.warning("[hermes] failed to record offer timestamp for %s: %s", chat_id, e)
