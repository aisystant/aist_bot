"""Smoke tests: voice_router (WP-384, DP.SC.178).

Voice message -> speech-to-text -> echo + note in the personal guide (when connected)
-> the same consultation path a typed "?" question takes.
"""

import io
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.github_app import WriteResult
from handlers.voice import TranscriptionError, on_voice, on_voice_consent_grant

_INSTALLATION = {"app_repo_full_name": "owner/personal-guide", "app_installation_id": "42"}


def _voice_message(duration: int = 10, chat_type: str = "private") -> MagicMock:
    """Minimal Message mock carrying a voice message."""
    msg = MagicMock()
    msg.chat.id = 12345
    msg.chat.type = chat_type
    msg.from_user.id = 12345
    msg.voice.duration = duration
    msg.voice.file_id = "voice_file_id"
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.download = AsyncMock(return_value=io.BytesIO(b"fake-ogg-bytes"))
    msg.model_copy = MagicMock(side_effect=lambda update: MagicMock(text=update["text"], voice=update["voice"]))
    return msg


@contextmanager
def _ready_user(installation=_INSTALLATION, transcript="тестовая расшифровка", routed=True):
    """Account linked, consent given, STT configured; yields the patched collaborators."""
    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value="ory-uuid-123"), \
         patch("handlers.voice.get_consent_grant", new_callable=AsyncMock, return_value=True), \
         patch("handlers.voice.STT_API_KEY", "stt-test"), \
         patch("handlers.voice.get_app_installation", new_callable=AsyncMock, return_value=installation), \
         patch("handlers.voice._transcribe_voice", new_callable=AsyncMock, return_value=transcript) as stt, \
         patch("handlers.voice.write_file", new_callable=AsyncMock,
               return_value=WriteResult(success=True, sha="abc123")) as write, \
         patch("handlers.voice._answer_as_typed_question", new_callable=AsyncMock, return_value=routed) as answer:
        yield stt, write, answer


@pytest.mark.asyncio
async def test_voice_ignored_in_group():
    msg = _voice_message(chat_type="supergroup")
    await on_voice(msg)
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_rejects_too_long():
    msg = _voice_message(duration=120)
    await on_voice(msg)
    msg.answer.assert_awaited_once()
    assert "длиннее" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_voice_requires_account():
    msg = _voice_message()
    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value=None):
        await on_voice(msg)
    msg.answer.assert_awaited_once()
    assert "Подключить аккаунт" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_voice_requires_consent():
    msg = _voice_message()
    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value="ory-uuid-123"), \
         patch("handlers.voice.get_consent_grant", new_callable=AsyncMock, return_value=False):
        await on_voice(msg)
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    assert "согласие" in args[0].lower()
    assert "OpenRouter" in args[0]
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "voice_consent_grant"
    msg.bot.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_happy_path_saves_note_echoes_and_routes_as_question():
    msg = _voice_message()
    with _ready_user() as (stt, write, answer):
        await on_voice(msg)

    stt.assert_awaited_once_with(b"fake-ogg-bytes")
    write_kwargs = write.call_args.kwargs
    assert write_kwargs["repo_full_name"] == "owner/personal-guide"
    assert write_kwargs["installation_id"] == 42
    assert write_kwargs["path"].startswith("history/") and write_kwargs["path"].endswith(".md")
    assert "тестовая расшифровка" in write_kwargs["content"]
    assert "voice-log" in write_kwargs["content"]

    echo = msg.answer.call_args_list[0][0][0]
    assert "тестовая расшифровка" in echo
    assert "Записал заметкой" in echo
    answer.assert_awaited_once_with(msg, "тестовая расшифровка")
    assert msg.answer.await_count == 1


@pytest.mark.asyncio
async def test_voice_without_personal_guide_still_answers():
    msg = _voice_message()
    with _ready_user(installation=None) as (_stt, write, answer):
        await on_voice(msg)

    write.assert_not_awaited()
    assert "не подключено" in msg.answer.call_args_list[0][0][0]
    answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_not_routed_tells_user_to_type():
    msg = _voice_message()
    with _ready_user(routed=False):
        await on_voice(msg)
    assert msg.answer.await_count == 2
    assert "текстом" in msg.answer.call_args_list[1][0][0]


@pytest.mark.asyncio
async def test_voice_transcription_failure_falls_back_to_text():
    msg = _voice_message()
    with _ready_user() as (stt, write, answer):
        stt.side_effect = TranscriptionError("STT HTTP 502")
        await on_voice(msg)
    msg.answer.assert_awaited_once()
    assert "Не смог распознать" in msg.answer.call_args[0][0]
    write.assert_not_awaited()
    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_empty_transcript():
    msg = _voice_message()
    with _ready_user(transcript="") as (_stt, write, answer):
        await on_voice(msg)
    assert "пустая" in msg.answer.call_args[0][0]
    write.assert_not_awaited()
    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_without_stt_key_reports_unavailable():
    msg = _voice_message()
    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value="ory-uuid-123"), \
         patch("handlers.voice.get_consent_grant", new_callable=AsyncMock, return_value=True), \
         patch("handlers.voice.STT_API_KEY", None):
        await on_voice(msg)
    assert "недоступны" in msg.answer.call_args[0][0]
    msg.bot.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_answer_as_typed_question_routes_prefixed_text():
    from handlers.voice import _answer_as_typed_question

    msg = _voice_message()
    dispatcher = MagicMock(is_sm_active=True, route_message=AsyncMock())
    intern = {"onboarding_completed": True}
    with patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("handlers.voice.get_intern", new_callable=AsyncMock, return_value=intern):
        routed = await _answer_as_typed_question(msg, "что у меня в плане")

    assert routed is True
    routed_intern, routed_msg = dispatcher.route_message.call_args[0]
    assert routed_intern is intern
    assert routed_msg.text == "?что у меня в плане"
    assert routed_msg.voice is None


@pytest.mark.asyncio
async def test_answer_as_typed_question_skips_not_onboarded():
    from handlers.voice import _answer_as_typed_question

    msg = _voice_message()
    dispatcher = MagicMock(is_sm_active=True, route_message=AsyncMock())
    with patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("handlers.voice.get_intern", new_callable=AsyncMock, return_value={"onboarding_completed": False}):
        assert await _answer_as_typed_question(msg, "вопрос") is False
    dispatcher.route_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_consent_grant_callback():
    cb = MagicMock()
    cb.from_user.id = 12345
    cb.data = "voice_consent_grant"
    cb.answer = AsyncMock()
    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value="ory-uuid-123"), \
         patch("handlers.voice.set_consent_grant", new_callable=AsyncMock) as mock_set:
        await on_voice_consent_grant(cb)

    kwargs = mock_set.call_args.kwargs
    assert kwargs["account_id"] == "ory-uuid-123"
    assert kwargs["scope"] == "voice_processing"
    assert kwargs["granted"] is True
    cb.answer.assert_awaited_once()
