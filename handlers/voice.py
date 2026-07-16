"""WP-384: voice messages -> text -> note + the same answer a typed question gets.

Implements DP.SC.178 "IWE voice channel (input)":
- voice is an edge channel: the transcript takes the same path as a typed
  "?" question (consultation via the state machine), no voice-only logic in the core;
- always-capture: the transcript is saved as a note in the personal guide when one
  is connected; without it the user still gets the echo and the answer;
- the audio is not stored (transcribe-and-discard) and the transcript is not logged;
- a separate consent gate for voice processing (scope voice_processing).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clients.github_app import write_file
from config import STT_API_KEY, STT_BASE_URL, STT_MODEL, VOICE_MAX_DURATION_SEC
from db.queries import get_intern
from db.queries.consent import get_consent_grant, set_consent_grant
from db.queries.github_app import get_app_installation
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

voice_router = Router(name="voice")

_VOICE_CONSENT_SCOPE = "voice_processing"
_VOICE_CONSENT_VERSION = "v1.1"
_STT_TIMEOUT_SEC = 30
# Same trigger as the "consultation" global event in config/transitions.yaml.
_QUESTION_PREFIX = "?"


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


async def _save_voice_note(chat_id: int, transcript: str) -> bool:
    """Save the transcript as a note in the personal guide. False when not saved."""
    installation = await get_app_installation(chat_id)
    if not installation:
        return False
    repo = installation.get("app_repo_full_name")
    installation_id = installation.get("app_installation_id")
    if not repo or not installation_id:
        return False

    now = datetime.now(timezone(timedelta(hours=3)))
    date_str = now.strftime("%Y-%m-%d")
    path = f"history/{date_str}-voice-{now.strftime('%H%M%S')}.md"
    content = (
        f"# Голосовая заметка — {date_str}\n\n"
        f"{transcript}\n\n"
        f"---\n"
        f"*Источник: voice* | *Тег: voice-log*\n"
    )
    result = await write_file(
        installation_id=int(installation_id),
        repo_full_name=repo,
        path=path,
        content=content,
        message=f"voice: {date_str} — заметка из бота",
    )
    if not result.success:
        logger.warning("[voice] save note failed for chat_id=%s: %s", chat_id, result.error)
    return bool(result.success)


async def _answer_as_typed_question(message: Message, transcript: str) -> bool:
    """Route the transcript exactly like a typed "?" question. False when not routed."""
    from handlers import get_dispatcher

    dispatcher = get_dispatcher()
    intern = await get_intern(message.chat.id)
    if not dispatcher or not dispatcher.is_sm_active or not intern or not intern.get("onboarding_completed"):
        return False
    as_text = message.model_copy(update={"text": f"{_QUESTION_PREFIX}{transcript}", "voice": None})
    await dispatcher.route_message(intern, as_text)
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


@voice_router.message(F.content_type == ContentType.VOICE)
async def on_voice(message: Message) -> None:
    """Voice message -> echo of the transcript -> note -> answer."""
    if message.chat.type in ("channel", "group", "supergroup") or message.from_user is None:
        return
    chat_id = message.from_user.id
    voice = message.voice
    if not voice:
        return

    if (voice.duration or 0) > VOICE_MAX_DURATION_SEC:
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
            "Запись распознаётся сервисом распознавания речи OpenAI (через OpenRouter) "
            "и не хранится. Текст расшифровки сохранится заметкой в твоём личном руководстве, "
            "если оно подключено, а на сам вопрос ответит Наставник.\n\n"
            "Нужно твоё согласие на обработку голоса.",
            reply_markup=_consent_keyboard(),
            parse_mode="HTML",
        )
        return

    if not STT_API_KEY:
        logger.error("[voice] STT_API_KEY is not configured")
        await message.answer("⚠️ Голосовые временно недоступны. Напиши текстом или попробуй позже.")
        return

    try:
        downloaded = await message.bot.download(voice)
        audio = downloaded.read() if downloaded else b""
    except Exception:
        logger.exception("[voice] download failed for chat_id=%s", chat_id)
        await message.answer("⚠️ Не удалось получить голосовое. Попробуй ещё раз.")
        return

    try:
        transcript = await _transcribe_voice(audio)
    except Exception as exc:
        logger.warning("[voice] transcription failed for chat_id=%s: %s", chat_id, type(exc).__name__)
        await message.answer("🎤 Не смог распознать голосовое. Повтори или напиши текстом.")
        return
    finally:
        del audio

    if not transcript:
        await message.answer("🎤 Расшифровка пустая. Скажи ещё раз или напиши текстом.")
        return

    saved = await _save_voice_note(chat_id, transcript)
    note_line = "Записал заметкой в личное руководство." if saved else "Заметку не сохранил: личное руководство не подключено (/settings)."
    await message.answer(f"🎤 {transcript}\n\n{note_line}")

    try:
        routed = await _answer_as_typed_question(message, transcript)
    except Exception:
        logger.exception("[voice] answer routing failed for chat_id=%s", chat_id)
        routed = False
    if not routed:
        await message.answer("Ответить на голосовое сейчас не получилось. Задай вопрос текстом, начав с «?».")
