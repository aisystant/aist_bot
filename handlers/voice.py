"""WP-384: voice messages -> text -> the same path the typed text would take.

Implements DP.SC.178 "IWE voice channel (input)":
- voice is an edge channel: the transcript is routed exactly like typed text,
  no voice-only logic in the core;
- default: the transcript becomes a fleeting note, the same as a typed ".text" note
  (handlers.github.process_fleeting_note -> core.fleeting_notes -> fleeting-notes.md in the
  user's notes repo), marked "голос" and with an undo button under «Записано»;
- «заметка …» forces a note, a transcript that starts with the word «вопрос» becomes a
  typed "?" question (consultation via the state machine);
- the audio is not stored and the transcript is not logged;
- a separate consent gate for voice processing (scope voice_processing); a voice message
  sent before the consent is processed right after the user agrees;
- only for chats allowed by core.voice_access; a voice message is not claimed when
  the bot is waiting for an answer (FSM state, marathon/feed reply, agent session)
  or when it replies to a message of the bot: those keep their previous handling;
- one [voice-metric] log line per voice message (outcome, duration, recognition time) and a
  "voice.stt" span in the request trace.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import aiohttp
from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ContentType
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clients.github_api import RemoveStatus, github_notes
from clients.github_auth import GitHubAuthUnavailable
from config import STT_API_KEY, STT_BASE_URL, STT_MODEL, VOICE_MAX_DURATION_SEC
from core.fleeting_notes import NoteSource
from core.tracing import span
from core.voice_access import voice_enabled_for
from db.queries import get_intern
from db.queries.consent import get_consent_grant, set_consent_grant
from helpers.dual_write import resolve_ory_id_from_chat
from integrations.telegram.keyboards import NOTE_UNDO_PREFIX

logger = logging.getLogger(__name__)

voice_router = Router(name="voice")

_VOICE_CONSENT_SCOPE = "voice_processing"
_VOICE_CONSENT_VERSION = "v1.3"
_STT_TIMEOUT_SEC = 30
# Typed-text triggers: ".text" saves a fleeting note, "?text" asks a question
# (the "consultation" global event in config/transitions.yaml).
_NOTE_PREFIX = "."
_QUESTION_PREFIX = "?"
_SPOKEN_QUESTION_RE = re.compile(r"^\s*вопрос\b[\s,.:;!?\-–—]*", re.IGNORECASE)
_SPOKEN_NOTE_RE = re.compile(r"^\s*заметка\b[\s,.:;!?\-–—]*", re.IGNORECASE)


class TranscriptionError(RuntimeError):
    """The speech-to-text provider did not return a transcript."""


@dataclass
class _VoiceRun:
    """What one voice message did, for the single metric line written when it is done."""

    voice_sec: int
    outcome: str = "aborted"
    stt_ms: int | None = None
    chars: int = 0


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


def _split_spoken(pattern: re.Pattern[str], transcript: str) -> str | None:
    """Text after a spoken marker word at the start, None when there is no marker or nothing after it."""
    match = pattern.match(transcript)
    if not match:
        return None
    return transcript[match.end():].strip() or None


def split_spoken_question(transcript: str) -> str | None:
    """Question text when the transcript starts with «вопрос», otherwise None."""
    return _split_spoken(_SPOKEN_QUESTION_RE, transcript)


def split_spoken_note(transcript: str) -> str | None:
    """Note text when the transcript starts with «заметка», otherwise None."""
    return _split_spoken(_SPOKEN_NOTE_RE, transcript)


def _is_reply_to_bot(message: Message) -> bool:
    replied = message.reply_to_message
    return bool(replied and replied.from_user and replied.from_user.is_bot)


def voice_claimable(message: Message) -> bool:
    """Router filter: a private voice message of an allowed user, not a reply to the bot."""
    return (
        message.chat.type == "private"
        and message.from_user is not None
        and message.voice is not None
        and not _is_reply_to_bot(message)
        and voice_enabled_for(message.from_user.id)
    )


async def _bot_awaits_reply(chat_id: int) -> bool:
    """The marathon or the feed is waiting for this user's answer (same check as the "Гермес" prefix)."""
    from handlers.external_session import _sm_is_expecting_reply
    from states.feed.digest import FeedDigestState

    return await _sm_is_expecting_reply(chat_id) or FeedDigestState.is_waiting_fixation(chat_id)


def _as_typed(message: Message, text: str) -> Message:
    """The same message as if the user had typed `text` instead of speaking."""
    return message.model_copy(update={"text": text, "voice": None})


async def _save_as_note(message: Message, text: str) -> None:
    """Route the text exactly like a typed ".text" fleeting note, marked as spoken."""
    from handlers.github import process_fleeting_note

    await process_fleeting_note(_as_typed(message, f"{_NOTE_PREFIX}{text}"), NoteSource.VOICE)


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


_CONSENT_TEXT = (
    "🎤 <b>Голосовые сообщения</b>\n\n"
    "Запись распознаёт сервис OpenAI через OpenRouter, по их правилам хранения. Бот запись не хранит. "
    "Текст сохраняется заметкой туда же, куда заметки с точкой, и копия текста заметки передаётся "
    "платформе Aisystant, как у обычных заметок. Если начать голосовое со слова «вопрос», ответит "
    "Наставник; со слова «заметка» - точно запишется заметкой.\n\n"
    "Отключить голосовые можно командой /voice_off.\n\n"
    "Нужно твоё согласие на обработку голоса."
)


@voice_router.callback_query(F.data == "voice_consent_grant")
async def on_voice_consent_grant(callback: CallbackQuery) -> None:
    """The user gave consent: remember it and process the voice message that asked for it."""
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
    pending = callback.message.reply_to_message if callback.message else None
    resume = bool(pending and pending.voice and pending.from_user and pending.from_user.id == chat_id)
    await callback.answer(
        "✅ Голосовые включены. Обрабатываю это сообщение." if resume
        else "✅ Голосовые включены. Отправь голосовое сообщение.",
        show_alert=True,
    )
    if resume and voice_claimable(pending):
        await _handle_voice(pending)


@voice_router.message(Command("voice_off"))
async def cmd_voice_off(message: Message) -> None:
    """Withdraw the consent for voice processing."""
    if message.from_user is None:
        return
    account_id = await resolve_ory_id_from_chat(message.from_user.id)
    if not account_id:
        await message.answer("Аккаунт Aisystant не подключён, голосовые и так не обрабатываются.")
        return
    await set_consent_grant(
        account_id=account_id,
        scope=_VOICE_CONSENT_SCOPE,
        granted=False,
        consent_version=_VOICE_CONSENT_VERSION,
        interface="bot",
    )
    await message.answer("Голосовые отключены. Включить снова можно, отправив голосовое и нажав «Согласиться».")


@voice_router.callback_query(F.data.startswith(NOTE_UNDO_PREFIX))
async def on_note_undo(callback: CallbackQuery) -> None:
    """«Отменить» under a saved voice note: remove exactly that note from the file."""
    parsed = _parse_undo_ref(callback.data[len(NOTE_UNDO_PREFIX):])
    if parsed is None:
        await callback.answer("Не получилось отменить: кнопка устарела.", show_alert=True)
        return
    try:
        status = await github_notes.remove_note_block(callback.from_user.id, *parsed)
    except GitHubAuthUnavailable:
        await callback.answer("Нужно переподключить GitHub: /github", show_alert=True)
        return
    if status is RemoveStatus.FAILED:
        await callback.answer("Не получилось отменить. Попробуй ещё раз.", show_alert=True)
        return
    text = "↩️ Заметка отменена." if status is RemoveStatus.REMOVED else (
        "Не нашёл эту заметку в файле: её уже изменили или удалили."
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(text)


def _parse_undo_ref(ref: str) -> tuple[str, int] | None:
    """(block fingerprint, block lines) from the button data, None when malformed."""
    block_hash, _, length = ref.partition(":")
    if not re.fullmatch(r"[0-9a-f]{10}", block_hash) or not length.isdigit() or not 0 < int(length) <= 500:
        return None
    return block_hash, int(length)


async def _transcript_or_reply(message: Message, run: _VoiceRun) -> str | None:
    """Download and transcribe the voice; on failure tell the user and return None."""
    try:
        downloaded = await message.bot.download(message.voice)
        audio = downloaded.read() if downloaded else b""
    except Exception:
        logger.exception("[voice] download failed for chat_id=%s", message.chat.id)
        run.outcome = "download_failed"
        await message.answer("⚠️ Не удалось получить голосовое. Попробуй ещё раз.")
        return None
    started = time.monotonic()
    try:
        async with span("voice.stt", voice_sec=run.voice_sec, model=STT_MODEL):
            transcript = await _transcribe_voice(audio)
    except Exception as exc:
        run.stt_ms = int((time.monotonic() - started) * 1000)
        logger.warning("[voice] transcription failed for chat_id=%s: %s", message.chat.id, type(exc).__name__)
        run.outcome = "stt_failed"
        await message.answer("🎤 Не смог распознать голосовое. Повтори или напиши текстом.")
        return None
    run.stt_ms = int((time.monotonic() - started) * 1000)
    run.chars = len(transcript)
    if not transcript:
        run.outcome = "empty"
        await message.answer("🎤 Расшифровка пустая. Скажи ещё раз или напиши текстом.")
        return None
    return transcript


@voice_router.message(F.content_type == ContentType.VOICE, StateFilter(None), voice_claimable)
async def on_voice(message: Message) -> None:
    """Voice message of an allowed user, unless the bot waits for the user's answer."""
    if await _bot_awaits_reply(message.from_user.id):
        raise SkipHandler
    await _handle_voice(message)


async def _handle_voice(message: Message) -> None:
    """Gates, recognition, echo, then a note (default) or a question; writes one metric line."""
    run = _VoiceRun(voice_sec=message.voice.duration or 0)
    try:
        await _process_voice(message, run)
    finally:
        logger.info(
            "[voice-metric] outcome=%s voice_sec=%d stt_ms=%s chars=%d",
            run.outcome, run.voice_sec, run.stt_ms if run.stt_ms is not None else "-", run.chars,
        )


async def _process_voice(message: Message, run: _VoiceRun) -> None:
    chat_id = message.from_user.id

    if run.voice_sec > VOICE_MAX_DURATION_SEC:
        run.outcome = "too_long"
        await message.answer(
            f"⚠️ Голосовое длиннее {VOICE_MAX_DURATION_SEC} сек. Разбей на части или напиши текстом."
        )
        return

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        run.outcome = "no_account"
        await message.answer(
            "🔐 Для голосовых нужно подключить аккаунт Aisystant.\n\n"
            "Нажми /settings → «Подключить аккаунт» и повтори голосовое."
        )
        return

    if not await get_consent_grant(account_id, _VOICE_CONSENT_SCOPE):
        run.outcome = "no_consent"
        # A reply, so that «Согласиться» can find this voice message and process it.
        await message.reply(_CONSENT_TEXT, reply_markup=_consent_keyboard(), parse_mode="HTML")
        return

    if not STT_API_KEY:
        logger.error("[voice] STT_API_KEY is not configured")
        run.outcome = "stt_not_configured"
        await message.answer("⚠️ Голосовые временно недоступны. Напиши текстом или попробуй позже.")
        return

    transcript = await _transcript_or_reply(message, run)
    if transcript is None:
        return
    await message.answer(f"🎤 {transcript}")

    note_text = split_spoken_note(transcript)
    question = None if note_text else split_spoken_question(transcript)
    if question is None:
        run.outcome = "note"
        await _save_as_note(message, note_text or transcript)
        return
    try:
        routed = await _ask_as_question(message, question)
    except Exception:
        logger.exception("[voice] question routing failed for chat_id=%s", chat_id)
        routed = False
    run.outcome = "question" if routed else "question_unrouted"
    if not routed:
        await message.answer("Ответить на вопрос сейчас не получилось. Задай его текстом, начав с «?».")
