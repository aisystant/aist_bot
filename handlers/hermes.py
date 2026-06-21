"""WP-392 Ф3.1: Hermes-роутер.

Префикс «Гермес»/«hermes» ВСЕГДА адресует внешний Hermes-рантайм (Nous Research)
через gateway-mcp `hermes_chat` (DP.SC.167) для tier ≥ T3.

Для tier < T3 префикс «Гермес» маршрутизирует к Проводнику (DP.SC.169) —
онбординг-помощнику на Haiku с узким scope: тиры T1→T4 и следующий шаг.

Имя «Гермес»/«Hermes» зарезервировано за продуктом Nous Research. Наши собственные
артефакты (Claude-сессии, Session Memory Injector и т.д.) так НЕ называются.
"""

import io
import logging
import re
import time

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message

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

_LONG_ARTIFACT_THRESHOLD = 3500  # chars; above this → send_document (WP-428 Ф8)
_SUPPORTED_DOC_EXTENSIONS = (".txt", ".md")  # Layer 1 plain-text only; PDF = deferred
_MAX_DOC_SIZE = 500_000  # bytes — guard against huge uploads

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


def _is_hermes_document(message: Message) -> bool:
    """Document (file) with hermes prefix in caption — WP-428 Ф8 Layer 1."""
    if message.chat.type in ("channel", "group", "supergroup"):
        return False
    if not message.document:
        return False
    caption = (message.caption or "").lower().lstrip()
    return caption.startswith(_HERMES_PREFIXES)


async def _extract_document_text(message: Message) -> str | None:
    """Download .txt/.md attachment; return decoded text or None if unsupported.

    Ephemeral: file is read once per request, not stored anywhere.
    """
    doc = message.document
    if not doc:
        return None
    filename = (doc.file_name or "").lower()
    if not any(filename.endswith(ext) for ext in _SUPPORTED_DOC_EXTENSIONS):
        return None
    if doc.file_size and doc.file_size > _MAX_DOC_SIZE:
        return None
    if not message.bot:
        return None
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    return buf.getvalue().decode("utf-8", errors="replace")


async def _hermes_reply(message: Message, placeholder: Message, response: str) -> None:
    """Deliver Hermes response: edit placeholder, or send_document for long output."""
    if len(response) > _LONG_ARTIFACT_THRESHOLD:
        try:
            await placeholder.delete()
        except Exception:
            pass  # best-effort: answer_document must proceed regardless
        artifact = BufferedInputFile(response.encode("utf-8"), filename="hermes-response.txt")
        await message.answer_document(artifact, caption="Гермес: ответ в файле")
        return
    try:
        await placeholder.edit_text(response, parse_mode="Markdown")
    except Exception:
        logger.exception("[hermes] edit_text (Markdown) failed for chat %s", message.chat.id)
        try:
            await placeholder.edit_text(response)
        except Exception:
            logger.exception("[hermes] edit_text (plain) failed for chat %s", message.chat.id)
            try:
                await placeholder.delete()
            except Exception:
                pass  # best-effort
            await message.answer(response)


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

    state_data = await state.get_data()
    session_id = state_data.get(_HERMES_SESSION_KEY, f"tg-{user_id}")

    from clients.gateway_mcp import gateway_mcp
    placeholder = await message.answer("⌛ Гермес думает…")
    try:
        response = await gateway_mcp.hermes_chat(
            message=hermes_msg,
            telegram_user_id=chat_id,
            session_id=session_id,
        )
    except Exception:
        logger.exception("[hermes] hermes_chat failed for chat %s", chat_id)
        response = None

    await _hermes_reply(message, placeholder, response or _UNAVAILABLE_RUNTIME_MSG)


@hermes_router.message(_is_hermes_document)
async def on_hermes_document(message: Message, state: FSMContext) -> None:
    """«Гермес, <caption>» + .txt/.md attachment → inject file contents into hermes_chat.

    WP-428 Ф8: Layer 1 ephemeral file input. File is read once and not stored.
    """
    if message.from_user is None:
        return
    user_id = message.from_user.id
    chat_id = message.chat.id

    intern = await get_intern(chat_id)

    from handlers.external_session import _sm_is_expecting_reply
    from states.feed.digest import FeedDigestState
    if await _sm_is_expecting_reply(chat_id) or FeedDigestState.is_waiting_fixation(chat_id):
        raise SkipHandler

    if not intern or not intern.get("onboarding_completed"):
        await message.answer(_UNAVAILABLE_TIER_MSG)
        return

    from core.tier_detector import detect_ui_tier
    tier = await detect_ui_tier(chat_id)
    logger.info("[hermes:doc] chat_id=%s tier=%s", chat_id, tier)

    if tier < _TIER_REQUIRED:
        await message.answer(_UNAVAILABLE_TIER_MSG)
        return

    caption = message.caption or ""
    hermes_msg = re.sub(r"^(гермес|hermes)[,:\s]+", "", caption, flags=re.IGNORECASE).strip()

    doc_text = await _extract_document_text(message)
    if doc_text is None:
        supported = " ".join(_SUPPORTED_DOC_EXTENSIONS)
        await message.answer(f"Файл не удалось прочитать. Поддерживаются: {supported} (до 500 КБ).")
        return

    filename = (message.document.file_name or "document") if message.document else "document"
    hermes_msg = f"[File: {filename}]\n\n{doc_text}\n\n{hermes_msg}".strip()

    state_data = await state.get_data()
    session_id = state_data.get(_HERMES_SESSION_KEY, f"tg-{user_id}")

    from clients.gateway_mcp import gateway_mcp
    placeholder = await message.answer("⌛ Гермес думает…")
    try:
        response = await gateway_mcp.hermes_chat(
            message=hermes_msg,
            telegram_user_id=chat_id,
            session_id=session_id,
        )
    except Exception:
        logger.exception("[hermes:doc] hermes_chat failed for chat %s", chat_id)
        response = None

    await _hermes_reply(message, placeholder, response or _UNAVAILABLE_RUNTIME_MSG)
