"""WP-384 Ф2: голосовые сообщения → текст → заметка + best-effort ответ.

Реализует DP.SC.178 «Голосовой канал IWE (Talk Mode — ввод)»:
- голос = edge-канал, ядро не форкается;
- always-capture: расшифровка сохраняется как заметка в personal-guide;
- best-effort ответ через hermes-путь (DP.SC.169);
- аудио не хранится (transcribe-and-discard);
- отдельный consent-gate для обработки голоса (voice_processing).
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone

from openai import AsyncOpenAI
from aiogram import Router, F
from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clients.github_app import write_file
from clients.gateway_mcp import gateway_mcp
from config import OPENAI_API_KEY, VOICE_MAX_DURATION_SEC, WHISPER_MODEL
from db.queries.consent import get_consent_grant, set_consent_grant
from db.queries.github_app import get_app_installation
from helpers.dual_write import resolve_ory_id_from_chat
from handlers.hermes import _send_unavailable

logger = logging.getLogger(__name__)

voice_router = Router(name="voice")

_VOICE_CONSENT_SCOPE = "voice_processing"
_VOICE_CONSENT_VERSION = "v1.0"


async def _transcribe_voice(audio_buf: io.BytesIO) -> str:
    """Распознать голос через OpenAI Whisper.

    Аудио передаётся напрямую в OpenAI; расшифровка не логируется.
    """
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    audio_buf.name = "voice.ogg"
    audio_buf.seek(0)

    response = await client.audio.transcriptions.create(
        model=WHISPER_MODEL,
        file=audio_buf,
        response_format="text",
    )
    # response_format="text" возвращает str; на всякий случай приводим.
    return str(response).strip()


async def _save_voice_note(chat_id: int, transcript: str) -> tuple[bool, str | None]:
    """Сохранить расшифровку голоса как заметку в personal-guide.

    Returns:
        (success, path_or_error).
    """
    installation = await get_app_installation(chat_id)
    if not installation:
        return False, None

    repo = installation.get("app_repo_full_name")
    installation_id = installation.get("app_installation_id")
    if not repo or not installation_id:
        return False, None

    now = datetime.now(timezone(timedelta(hours=3)))
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")
    path = f"history/{date_str}-voice-{time_str}.md"

    file_content = (
        f"# Голосовая заметка — {date_str}\n\n"
        f"{transcript}\n\n"
        f"---\n"
        f"*Источник: voice* | *Тег: voice-log*\n"
    )

    result = await write_file(
        installation_id=int(installation_id),
        repo_full_name=repo,
        path=path,
        content=file_content,
        message=f"voice: {date_str} — заметка из бота",
    )
    if result.success:
        return True, path
    logger.warning("[voice] save note failed for chat_id=%s: %s", chat_id, result.error)
    return False, result.error


async def _reply_with_hermes(message: Message, transcript: str) -> None:
    """Отправить эхо + best-effort ответ через hermes-путь."""
    chat_id = message.chat.id

    if not gateway_mcp.is_connected(chat_id):
        await _send_unavailable(message, None, chat_id)
        return

    response = await gateway_mcp.hermes_chat(
        message=transcript,
        telegram_user_id=chat_id,
    )

    if response:
        text = f"🎤 Расшифровка:\n{transcript}\n\n{response}"
    else:
        text = (
            f"🎤 Расшифровка сохранена:\n{transcript}\n\n"
            "Если нужно действие — уточни текстом."
        )

    await message.answer(text)


@voice_router.callback_query(F.data == "voice_consent_grant")
async def on_voice_consent_grant(callback: CallbackQuery) -> None:
    """Пользователь дал согласие на обработку голоса inline-кнопкой."""
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
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
    await callback.answer("✅ Голосовая обработка включена. Отправь голосовое сообщение.", show_alert=True)


@voice_router.message(F.content_type == ContentType.VOICE)
async def on_voice(message: Message) -> None:
    """Главный обработчик голосовых сообщений."""
    if message.chat.type in ("channel", "group", "supergroup"):
        return

    chat_id = message.chat.id
    voice = message.voice
    if not voice:
        return

    # DP.SC.178: cap 60с (конфигурируемо).
    duration = voice.duration or 0
    if duration > VOICE_MAX_DURATION_SEC:
        await message.answer(
            f"⚠️ Голосовое сообщение слишком длинное (>{VOICE_MAX_DURATION_SEC} сек). "
            "Разбей на части или отправь текстом."
        )
        return

    # Проверяем аккаунт и отдельный consent-gate для голоса.
    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        await message.answer(
            "🔐 Для голосовых заметок нужно подключить аккаунт Aisystant.\n\n"
            "Нажми /settings → «Подключить аккаунт» и повтори голосовое."
        )
        return

    if not await get_consent_grant(account_id, _VOICE_CONSENT_SCOPE):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Согласиться",
                        callback_data="voice_consent_grant",
                    )
                ]
            ]
        )
        await message.answer(
            "🎤 <b>Голосовые сообщения</b>\n\n"
            "Аудио будет распознано через OpenAI Whisper и сразу удалено. "
            "Расшифровка сохранится как заметка в твоём персональном руководстве.\n\n"
            "Нужно твоё согласие на обработку голосовых данных.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Проверяем GitHub App до скачивания аудио — неэффективно качать впустую.
    installation = await get_app_installation(chat_id)
    if not installation:
        await message.answer(
            "📚 Для сохранения голосовых заметок нужно подключить личное руководство.\n\n"
            "Нажми /settings → «Подключи личное руководство» и повтори голосовое."
        )
        return

    if not OPENAI_API_KEY:
        logger.error("[voice] OPENAI_API_KEY not configured")
        await message.answer(
            "⚠️ Голосовой ввод временно недоступен: не настроен сервис распознавания. "
            "Напиши текстом или попробуй позже."
        )
        return

    # Скачиваем аудио в память, не сохраняем на диск.
    audio_buf = io.BytesIO()
    try:
        await message.bot.download(voice, destination=audio_buf)
    except Exception as exc:
        logger.exception("[voice] download failed for chat_id=%s", chat_id)
        await message.answer("⚠️ Не удалось получить голосовое сообщение. Попробуй ещё раз.")
        return

    # Распознаём.
    try:
        transcript = await _transcribe_voice(audio_buf)
    except Exception as exc:
        logger.warning("[voice] transcription failed for chat_id=%s: %s", chat_id, exc)
        await message.answer(
            "🎤 Не смог распознать голосовое сообщение. Повтори текстом — мысль не потеряется."
        )
        return

    if not transcript:
        await message.answer(
            "🎤 Расшифровка пустая. Попробуй говорить чётче или отправь текстом."
        )
        return

    # Сохраняем заметку.
    saved, info = await _save_voice_note(chat_id, transcript)
    if not saved:
        await message.answer(
            "🎤 Расшифровка:\n" + transcript + "\n\n"
            "⚠️ Не удалось сохранить заметку в персональное руководство. "
            "Проверь подключение в /settings."
        )
        return

    # Best-effort ответ.
    await _reply_with_hermes(message, transcript)
