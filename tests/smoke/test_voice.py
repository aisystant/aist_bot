"""Smoke tests: voice_router (WP-384, DP.SC.178).

Voice message -> speech-to-text -> echo -> the path typed text would take:
a ".text" fleeting note by default, a "?text" question when it starts with «вопрос».
"""

import io
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiogram.dispatcher.event.bases import SkipHandler

from handlers.voice import (
    TranscriptionError,
    on_voice,
    on_voice_consent_grant,
    split_spoken_question,
    voice_claimable,
)


@pytest.fixture(autouse=True)
def _bot_not_waiting_for_reply():
    with patch("handlers.voice._bot_awaits_reply", new_callable=AsyncMock, return_value=False) as waiting:
        yield waiting


def _voice_message(duration: int = 10, chat_type: str = "private") -> MagicMock:
    """Minimal Message mock carrying a voice message."""
    msg = MagicMock()
    msg.chat.id = 12345
    msg.chat.type = chat_type
    msg.from_user.id = 12345
    msg.reply_to_message = None
    msg.voice.duration = duration
    msg.voice.file_id = "voice_file_id"
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.download = AsyncMock(return_value=io.BytesIO(b"fake-ogg-bytes"))
    msg.model_copy = MagicMock(side_effect=lambda update: MagicMock(text=update["text"], voice=update["voice"]))
    return msg


@contextmanager
def _ready_user(transcript="купить молоко", routed=True):
    """Account linked, consent given, STT configured; yields the patched collaborators."""
    with patch("handlers.voice.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value="ory-uuid-123"), \
         patch("handlers.voice.get_consent_grant", new_callable=AsyncMock, return_value=True), \
         patch("handlers.voice.STT_API_KEY", "stt-test"), \
         patch("handlers.voice._transcribe_voice", new_callable=AsyncMock, return_value=transcript) as stt, \
         patch("handlers.github.handle_fleeting_note", new_callable=AsyncMock) as note, \
         patch("handlers.voice._ask_as_question", new_callable=AsyncMock, return_value=routed) as ask:
        yield stt, note, ask


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Вопрос: что у меня в плане?", "что у меня в плане?"),
        ("вопрос, как начать марафон", "как начать марафон"),
        ("ВОПРОС — где мои заметки", "где мои заметки"),
        ("вопрос", None),
        ("вопросы стратегии на декабрь", None),
        ("идея про экзоскелет мышления", None),
    ],
)
def test_split_spoken_question(transcript, expected):
    assert split_spoken_question(transcript) == expected


def _reply_to(is_bot: bool) -> MagicMock:
    replied = MagicMock()
    replied.from_user.is_bot = is_bot
    return replied


@pytest.mark.parametrize(
    ("chat_type", "reply_to", "allowed", "expected"),
    [
        ("private", None, True, True),
        ("private", "own", True, True),
        ("private", "bot", True, False),
        ("private", None, False, False),
        ("supergroup", None, True, False),
    ],
)
def test_voice_claimable(chat_type, reply_to, allowed, expected):
    msg = _voice_message(chat_type=chat_type)
    msg.reply_to_message = None if reply_to is None else _reply_to(is_bot=reply_to == "bot")
    with patch("handlers.voice.voice_enabled_for", return_value=allowed) as enabled:
        assert voice_claimable(msg) is expected
    if chat_type == "private" and reply_to != "bot":
        enabled.assert_called_once_with(12345)


@pytest.mark.asyncio
async def test_voice_skipped_when_bot_awaits_reply(_bot_not_waiting_for_reply):
    _bot_not_waiting_for_reply.return_value = True
    msg = _voice_message()
    with pytest.raises(SkipHandler):
        await on_voice(msg)
    msg.answer.assert_not_awaited()
    msg.bot.download.assert_not_awaited()


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
    assert "с точки" in args[0]
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "voice_consent_grant"
    msg.bot.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_default_is_dot_note():
    msg = _voice_message()
    with _ready_user(transcript="купить молоко") as (stt, note, ask):
        await on_voice(msg)

    stt.assert_awaited_once_with(b"fake-ogg-bytes")
    assert msg.answer.call_args_list[0][0][0] == "🎤 купить молоко"
    note.assert_awaited_once()
    typed = note.call_args[0][0]
    assert typed.text == ".купить молоко"
    assert typed.voice is None
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_starting_with_question_word_asks_mentor():
    msg = _voice_message()
    with _ready_user(transcript="Вопрос: как начать марафон") as (_stt, note, ask):
        await on_voice(msg)

    ask.assert_awaited_once_with(msg, "как начать марафон")
    note.assert_not_awaited()
    assert msg.answer.await_count == 1


@pytest.mark.asyncio
async def test_voice_question_not_routed_tells_user_to_type():
    msg = _voice_message()
    with _ready_user(transcript="вопрос где мои заметки", routed=False):
        await on_voice(msg)
    assert msg.answer.await_count == 2
    assert "текстом" in msg.answer.call_args_list[1][0][0]


@pytest.mark.asyncio
async def test_voice_transcription_failure_falls_back_to_text():
    msg = _voice_message()
    with _ready_user() as (stt, note, ask):
        stt.side_effect = TranscriptionError("STT HTTP 502")
        await on_voice(msg)
    msg.answer.assert_awaited_once()
    assert "Не смог распознать" in msg.answer.call_args[0][0]
    note.assert_not_awaited()
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_empty_transcript():
    msg = _voice_message()
    with _ready_user(transcript="") as (_stt, note, ask):
        await on_voice(msg)
    assert "пустая" in msg.answer.call_args[0][0]
    note.assert_not_awaited()
    ask.assert_not_awaited()


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
async def test_ask_as_question_routes_prefixed_text():
    from handlers.voice import _ask_as_question

    msg = _voice_message()
    dispatcher = MagicMock(is_sm_active=True, route_message=AsyncMock())
    intern = {"onboarding_completed": True}
    with patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("handlers.voice.get_intern", new_callable=AsyncMock, return_value=intern):
        routed = await _ask_as_question(msg, "что у меня в плане")

    assert routed is True
    routed_intern, routed_msg = dispatcher.route_message.call_args[0]
    assert routed_intern is intern
    assert routed_msg.text == "?что у меня в плане"
    assert routed_msg.voice is None


@pytest.mark.asyncio
async def test_ask_as_question_skips_not_onboarded():
    from handlers.voice import _ask_as_question

    msg = _voice_message()
    dispatcher = MagicMock(is_sm_active=True, route_message=AsyncMock())
    with patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("handlers.voice.get_intern", new_callable=AsyncMock, return_value={"onboarding_completed": False}):
        assert await _ask_as_question(msg, "вопрос") is False
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
