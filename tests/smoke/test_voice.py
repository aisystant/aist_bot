"""Smoke-тесты: voice_router (WP-384 Ф2, DP.SC.178).

Голосовое сообщение → Whisper → заметка в personal-guide + best-effort ответ.
"""

import io
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from clients.github_app import WriteResult
from handlers.voice import on_voice, on_voice_consent_grant


def _voice_message(duration: int = 10, chat_type: str = "private") -> MagicMock:
    """Минимальный mock Message с голосовым сообщением."""
    msg = MagicMock()
    msg.chat.id = 12345
    msg.chat.type = chat_type
    msg.from_user.id = 12345
    msg.voice.duration = duration
    msg.voice.file_id = "voice_file_id"
    msg.answer = AsyncMock()
    return msg


# ─── Unit-style: direct handler calls ───


@pytest.mark.asyncio
async def test_voice_ignored_in_group():
    """Голосовые в группах/каналах не обрабатываются."""
    msg = _voice_message(chat_type="supergroup")
    await on_voice(msg)
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_requires_account():
    """Без привязанного Ory-аккаунта — подсказка подключить аккаунт."""
    msg = _voice_message()

    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock) as mock_resolve:
        mock_resolve.return_value = None
        await on_voice(msg)

    msg.answer.assert_awaited_once()
    text = msg.answer.call_args[0][0]
    assert "Подключить аккаунт" in text


@pytest.mark.asyncio
async def test_voice_requires_consent():
    """Без согласия voice_processing — inline-кнопка согласия."""
    msg = _voice_message()

    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock) as mock_resolve, \
         patch("handlers.voice.get_consent_grant", new_callable=AsyncMock) as mock_consent:
        mock_resolve.return_value = "ory-uuid-123"
        mock_consent.return_value = False
        await on_voice(msg)

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    assert "согласие" in args[0].lower()
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_voice_requires_github_app():
    """Согласие есть, но GitHub App не установлен — подсказка подключить руководство."""
    msg = _voice_message()

    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock) as mock_resolve, \
         patch("handlers.voice.get_consent_grant", new_callable=AsyncMock) as mock_consent, \
         patch("handlers.voice.get_app_installation", new_callable=AsyncMock) as mock_app:
        mock_resolve.return_value = "ory-uuid-123"
        mock_consent.return_value = True
        mock_app.return_value = None
        await on_voice(msg)

    msg.answer.assert_awaited_once()
    text = msg.answer.call_args[0][0]
    assert "личное руководство" in text


@pytest.mark.asyncio
async def test_voice_rejects_too_long():
    """Аудио длиннее VOICE_MAX_DURATION_SEC отклоняется."""
    msg = _voice_message(duration=120)
    await on_voice(msg)
    msg.answer.assert_awaited_once()
    text = msg.answer.call_args[0][0]
    assert "слишком длинное" in text


@pytest.mark.asyncio
async def test_voice_happy_path():
    """Полный проход: скачивание → транскрипция → заметка → ответ."""
    msg = _voice_message(duration=10)

    async def fake_download(voice_obj, destination):
        destination.write(b"fake-ogg-bytes")

    msg.bot = MagicMock()
    msg.bot.download = AsyncMock(side_effect=fake_download)

    mock_openai_client = MagicMock()
    mock_openai_client.audio.transcriptions.create = AsyncMock(return_value="тестовая расшифровка")

    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock) as mock_resolve, \
         patch("handlers.voice.get_consent_grant", new_callable=AsyncMock) as mock_consent, \
         patch("handlers.voice.get_app_installation", new_callable=AsyncMock) as mock_app, \
         patch("handlers.voice.OPENAI_API_KEY", "sk-test"), \
         patch("handlers.voice.AsyncOpenAI", return_value=mock_openai_client) as mock_openai_cls, \
         patch("handlers.voice.write_file", new_callable=AsyncMock) as mock_write, \
         patch("handlers.voice.gateway_mcp") as mock_gw:
        mock_resolve.return_value = "ory-uuid-123"
        mock_consent.return_value = True
        mock_app.return_value = {
            "app_repo_full_name": "owner/personal-guide",
            "app_installation_id": "42",
        }
        mock_write.return_value = WriteResult(success=True, sha="abc123")
        mock_gw.is_connected.return_value = True
        mock_gw.hermes_chat = AsyncMock(return_value="ответ ассистента")

        await on_voice(msg)

    # Whisper вызван с OGG-данными.
    mock_openai_cls.assert_called_once_with(api_key="sk-test")
    mock_openai_client.audio.transcriptions.create.assert_awaited_once()
    create_call = mock_openai_client.audio.transcriptions.create.call_args
    assert create_call.kwargs["model"] == "whisper-1"

    # Заметка сохранена.
    mock_write.assert_awaited_once()
    write_kwargs = mock_write.call_args.kwargs
    assert write_kwargs["repo_full_name"] == "owner/personal-guide"
    assert write_kwargs["installation_id"] == 42
    assert write_kwargs["path"].startswith("history/")
    assert write_kwargs["path"].endswith(".md")
    assert "тестовая расшифровка" in write_kwargs["content"]
    assert "voice-log" in write_kwargs["content"]

    # Пользователю отправлено эхо + ответ.
    msg.answer.assert_awaited_once()
    reply_text = msg.answer.call_args[0][0]
    assert "🎤 Расшифровка:" in reply_text
    assert "тестовая расшифровка" in reply_text
    assert "ответ ассистента" in reply_text


@pytest.mark.asyncio
async def test_voice_transcribe_error_fallback():
    """Ошибка Whisper → текстовый fallback, не тишина."""
    msg = _voice_message(duration=10)
    msg.bot = MagicMock()
    msg.bot.download = AsyncMock(side_effect=lambda voice_obj, destination: destination.write(b"x"))

    mock_openai_client = MagicMock()
    mock_openai_client.audio.transcriptions.create = AsyncMock(side_effect=RuntimeError("whisper down"))

    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock) as mock_resolve, \
         patch("handlers.voice.get_consent_grant", new_callable=AsyncMock) as mock_consent, \
         patch("handlers.voice.get_app_installation", new_callable=AsyncMock) as mock_app, \
         patch("handlers.voice.OPENAI_API_KEY", "sk-test"), \
         patch("handlers.voice.AsyncOpenAI", return_value=mock_openai_client):
        mock_resolve.return_value = "ory-uuid-123"
        mock_consent.return_value = True
        mock_app.return_value = {
            "app_repo_full_name": "owner/personal-guide",
            "app_installation_id": "42",
        }
        await on_voice(msg)

    msg.answer.assert_awaited_once()
    text = msg.answer.call_args[0][0]
    assert "Не смог распознать" in text


@pytest.mark.asyncio
async def test_voice_consent_grant_callback():
    """Нажатие inline-кнопки «Согласиться» записывает voice_processing consent."""
    cb = MagicMock()
    cb.message.chat.id = 12345
    cb.from_user.id = 12345
    cb.data = "voice_consent_grant"
    cb.answer = AsyncMock()

    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock) as mock_resolve, \
         patch("handlers.voice.set_consent_grant", new_callable=AsyncMock) as mock_set:
        mock_resolve.return_value = "ory-uuid-123"
        await on_voice_consent_grant(cb)

    mock_set.assert_awaited_once()
    kwargs = mock_set.call_args.kwargs
    assert kwargs["account_id"] == "ory-uuid-123"
    assert kwargs["scope"] == "voice_processing"
    assert kwargs["granted"] is True
    cb.answer.assert_awaited_once()
