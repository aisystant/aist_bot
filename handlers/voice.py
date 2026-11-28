"""WP-384: voice messages -> text -> the same path the typed text would take.

Implements DP.SC.178 "IWE voice channel (input)":
- voice is an edge channel: the transcript is routed exactly like typed text,
  no voice-only logic in the core;
- default: the transcript becomes a fleeting note, the same as a typed ".text" note
  (handlers.github.handle_fleeting_note -> fleeting-notes.md in the user's repo);
- a transcript that starts with the word «вопрос» becomes a typed "?" question
  (consultation via the state machine);
- the audio is not stored and the transcript is not logged;
- a separate consent gate for voice processing (scope voice_processing).
"""

from __future__ import annotations

import logging
import re

import aiohttp
from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import STT_API_KEY, STT_BASE_URL, STT_MODEL, VOICE_MAX_DURATION_SEC
from db.queries import get_intern
from db.queries.consent import get_consent_grant, set_consent_grant
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

voice_router = Router(name="voice")

_VOICE_CONSENT_SCOPE = "voice_processing"
_VOICE_CONSENT_VERSION = "v1.2"
_STT_TIMEOUT_SEC = 30
# Typed-text triggers: ".text" saves a fleeting note, "?text" asks a question
# (the "consultation" global event in config/transitions.yaml).
_NOTE_PREFIX = "."
_QUESTION_PREFIX = "?"
_SPOKEN_QUESTION_RE = re.compile(r"^\s*вопрос\b[\s,.:;!?\-–—]*", re.IGNORECASE)


class TranscriptionError(RuntimeError):
    """The speech-to-text provider did not return a transcript."""


async def _transcribe_voice(audio: bytes) -> str:
    """Send the audio to the OpenAI-compatible /audio/transcriptions endpoint.

    The transcript is returned to the caller only; it is never logged.
    """
    form = aiohttp.FormData()
    form.add_field("model", STT_MODEL)
    form.add_field("file", audio, filename="voice.ogg", content_type="audio/ogg")
    timeout = aiohttp.ClientTimeout(total=_STT_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{STT_BASE_URL.rstrip('/')}/audio/transcriptions",
            data=form,
            headers={"Authorization": f"Bearer {STT_API_KEY}"},
        ) as resp:
            if resp.status != 200:
                raise TranscriptionError(f"STT HTTP {resp.status}")
            data = await resp.json()
    return str(data.get("text") or "").strip()


def split_spoken_question(transcript: str) -> str | None:
    """Question text when the transcript starts with «вопрос», otherwise None."""
    match = _SPOKEN_QUESTION_RE.match(transcript)
    if not match:
        return None
    return transcript[match.end():].strip() or None


def _as_typed(message: Message, text: str) -> Message:
    """The same message as if the user had typed `text` instead of speaking."""
    return message.model_copy(update={"text": text, "voice": None})


async def _save_as_note(message: Message, transcript: str) -> None:
    """Route the transcript exactly like a typed ".text" fleeting note."""
    from handlers.github import handle_fleeting_note

    await handle_fleeting_note(_as_typed(message, f"{_NOTE_PREFIX}{transcript}"))


async def _ask_as_question(message: Message, question: str) -> bool:
    """Route the question exactly like a typed "?" question. False when not routed."""
    from handlers import get_dispatcher

    dispatcher = get_dispatcher()
    intern = await get_intern(message.chat.id)
    if not dispatcher or not dispatcher.is_sm_active or not intern or not intern.get("onboarding_completed"):
        return False
    await dispatcher.route_message(intern, _as_typed(message, f"{_QUESTION_PREFIX}{question}"))
    return True


def _consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Согласиться", callback_data="voice_consent_grant")]]
    )


@voice_router.callback_query(F.data == "voice_consent_grant")
async def on_voice_consent_grant(callback: CallbackQuery) -> None:
    """The user gave consent for voice processing with the inline button."""
    chat_id = callback.from_user.id
    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        await callback.answer("Сначала подключи аккаунт через /settings", show_alert=True)
        return
    await set_consent_grant(
        account_id=account_id,
        scope=_VOICE_CONSENT_SCOPE,
        granted=True,
        consent_version=_VOICE_CONSENT_VERSION,
        interface="bot",
    )
    await callback.answer("✅ Голосовые включены. Отправь голосовое сообщение.", show_alert=True)


async def _transcript_or_reply(message: Message) -> str | None:
    """Download and transcribe the voice; on failure tell the user and return None."""
    try:
        downloaded = await message.bot.download(message.voice)
        audio = downloaded.read() if downloaded else b""
    except Exception:
        logger.exception("[voice] download failed for chat_id=%s", message.chat.id)
        await message.answer("⚠️ Не удалось получить голосовое. Попробуй ещё раз.")
        return None
    try:
        transcript = await _transcribe_voice(audio)
    except Exception as exc:
        logger.warning("[voice] transcription failed for chat_id=%s: %s", message.chat.id, type(exc).__name__)
        await message.answer("🎤 Не смог распознать голосовое. Повтори или напиши текстом.")
        return None
    if not transcript:
        await message.answer("🎤 Расшифровка пустая. Скажи ещё раз или напиши текстом.")
        return None
    return transcript


@voice_router.message(F.content_type == ContentType.VOICE)
async def on_voice(message: Message) -> None:
    """Voice message -> echo of the transcript -> note (default) or question («вопрос …»)."""
    if message.chat.type in ("channel", "group", "supergroup") or message.from_user is None or not message.voice:
        return
    chat_id = message.from_user.id

    if (message.voice.duration or 0) > VOICE_MAX_DURATION_SEC:
        await message.answer(
            f"⚠️ Голосовое длиннее {VOICE_MAX_DURATION_SEC} сек. Разбей на части или напиши текстом."
        )
        return

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        await message.answer(
            "🔐 Для голосовых нужно подключить аккаунт Aisystant.\n\n"
            "Нажми /settings → «Подключить аккаунт» и повтори голосовое."
        )
        return

    if not await get_consent_grant(account_id, _VOICE_CONSENT_SCOPE):
        await message.answer(
            "🎤 <b>Голосовые сообщения</b>\n\n"
            "Запись распознаётся сервисом распознавания речи OpenAI (через OpenRouter) и не хранится. "
            "Текст сохраняется заметкой туда же, куда заметки, начатые с точки. "
            "Если начать голосовое со слова «вопрос», ответит Наставник.\n\n"
            "Нужно твоё согласие на обработку голоса.",
            reply_markup=_consent_keyboard(),
            parse_mode="HTML",
        )
        return

    if not STT_API_KEY:
        logger.error("[voice] STT_API_KEY is not configured")
        await message.answer("⚠️ Голосовые временно недоступны. Напиши текстом или попробуй позже.")
        return

    transcript = await _transcript_or_reply(message)
    if transcript is None:
        return
    await message.answer(f"🎤 {transcript}")

    question = split_spoken_question(transcript)
    if question is None:
        await _save_as_note(message, transcript)
        return
    try:
        routed = await _ask_as_question(message, question)
    except Exception:
        logger.exception("[voice] question routing failed for chat_id=%s", chat_id)
        routed = False
    if not routed:
        await message.answer("Ответить на вопрос сейчас не получилось. Задай его текстом, начав с «?».")
