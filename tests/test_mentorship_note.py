"""
WP-578 Ф3 — /mentor_note: наставник отвечает на сообщение участника, бот
просит подтвердить отсутствие чужих личных данных, затем зовёт
add_participant_note mentorship-service (бот = тонкий клиент, сам в базу
наставничества не пишет).
"""

import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from db.queries.mentorship import StreamChatContext
from clients.mentorship_service import MentorshipServiceError


def _make_message(*, reply_to_message=None, from_user_id=100, text="/mentor_note", args=None):
    from aiogram.types import Message, User, Chat

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = from_user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = -1001
    msg.chat.type = "group"
    msg.text = text
    msg.reply_to_message = reply_to_message
    msg.reply = AsyncMock()
    msg.bot = AsyncMock()
    command = MagicMock()
    command.args = args
    return msg, command


def _make_target_user(user_id: int, full_name: str = "Участник Тестов", text: str = "переслал скриншот"):
    from aiogram.types import Message, User

    target = MagicMock(spec=Message)
    target.from_user = MagicMock(spec=User)
    target.from_user.id = user_id
    target.from_user.full_name = full_name
    target.text = text
    target.caption = None
    return target


def _make_callback(data: str, *, chat_id=-1001, from_user_id=100):
    from aiogram.types import CallbackQuery, User, Message, Chat

    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = MagicMock(spec=User)
    callback.from_user.id = from_user_id
    callback.message = MagicMock(spec=Message)
    callback.message.chat = MagicMock(spec=Chat)
    callback.message.chat.id = chat_id
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.fixture(autouse=True)
def _clear_pending_notes():
    import handlers.mentorship as mentorship

    mentorship._pending_notes.clear()
    yield
    mentorship._pending_notes.clear()


@pytest.mark.asyncio
async def test_note_without_reply_target_asks_to_reply():
    import handlers.mentorship as mentorship

    message, command = _make_message(reply_to_message=None)
    await mentorship.cmd_mentor_note(message, command)

    message.reply.assert_awaited_once()
    assert "ответь" in message.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_note_self_is_rejected():
    import handlers.mentorship as mentorship

    target = _make_target_user(100)  # тот же id, что и у наставника
    message, command = _make_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note(message, command)

    message.reply.assert_awaited_once_with("Нельзя сохранить собственное сообщение наставника как заметку об участнике.")


@pytest.mark.asyncio
async def test_note_rejects_non_stream_reader(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value="11111111-1111-1111-1111-111111111111"))
    monkeypatch.setattr(
        mentorship,
        "lookup_stream_chat",
        AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id="11111111-1111-1111-1111-111111111111")),
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value=None))

    target = _make_target_user(200)
    message, command = _make_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note(message, command)

    assert "не числишься наставником" in message.reply.await_args.args[0]
    assert (message.chat.id, 100) not in mentorship._pending_notes


@pytest.mark.asyncio
async def test_note_stores_pending_and_asks_confirmation(monkeypatch):
    import handlers.mentorship as mentorship

    mentor_id = "11111111-1111-1111-1111-111111111111"
    participant_id = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=[mentor_id, participant_id]))
    monkeypatch.setattr(
        mentorship, "lookup_stream_chat", AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id=mentor_id))
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))

    target = _make_target_user(200, full_name="Иван Иванов", text="переслал скриншот участника")
    message, command = _make_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note(message, command)

    pending = mentorship._pending_notes[(message.chat.id, 100)]
    assert pending.body == "переслал скриншот участника"
    assert pending.participant_account_id == participant_id
    reply_text = message.reply.await_args.args[0]
    assert "Иван Иванов" in reply_text
    assert "переслал скриншот участника" in reply_text


@pytest.mark.asyncio
async def test_note_command_arg_overrides_target_text(monkeypatch):
    import handlers.mentorship as mentorship

    mentor_id = "11111111-1111-1111-1111-111111111111"
    participant_id = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=[mentor_id, participant_id]))
    monkeypatch.setattr(
        mentorship, "lookup_stream_chat", AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id=mentor_id))
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))

    target = _make_target_user(200, text="исходный текст сообщения")
    message, command = _make_message(reply_to_message=target, from_user_id=100, args="явный текст заметки наставника")

    await mentorship.cmd_mentor_note(message, command)

    pending = mentorship._pending_notes[(message.chat.id, 100)]
    assert pending.body == "явный текст заметки наставника"


@pytest.mark.asyncio
async def test_note_cancel_discards_pending_without_calling_service(monkeypatch):
    import handlers.mentorship as mentorship

    mentorship._pending_notes[(-1001, 100)] = mentorship._PendingNote(
        mentor_account_id="11111111-1111-1111-1111-111111111111",
        stream_id="S1",
        participant_account_id="22222222-2222-2222-2222-222222222222",
        participant_name="Иван Иванов",
        body="текст",
        created_at=0.0,
    )
    add_note_mock = AsyncMock()
    monkeypatch.setattr(mentorship.mentorship_service, "add_participant_note", add_note_mock)

    callback = _make_callback("mentor_note:cancel", chat_id=-1001, from_user_id=100)
    await mentorship.cb_mentor_note(callback)

    add_note_mock.assert_not_called()
    callback.message.edit_text.assert_awaited_once_with("Отменено — заметка не сохранена.")
    assert (-1001, 100) not in mentorship._pending_notes


@pytest.mark.asyncio
async def test_note_confirm_calls_service_and_confirms(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "time", lambda: 1000.0)
    mentorship._pending_notes[(-1001, 100)] = mentorship._PendingNote(
        mentor_account_id="11111111-1111-1111-1111-111111111111",
        stream_id="S1",
        participant_account_id="22222222-2222-2222-2222-222222222222",
        participant_name="Иван Иванов",
        body="переслал скриншот",
        created_at=1000.0,
    )
    add_note_mock = AsyncMock(return_value={"participantId": 42, "noteId": 7})
    monkeypatch.setattr(mentorship.mentorship_service, "add_participant_note", add_note_mock)

    callback = _make_callback("mentor_note:confirm", chat_id=-1001, from_user_id=100)
    await mentorship.cb_mentor_note(callback)

    add_note_mock.assert_awaited_once_with(
        "11111111-1111-1111-1111-111111111111",
        "S1",
        "22222222-2222-2222-2222-222222222222",
        "переслал скриншот",
        source_hint="telegram-forward",
        quarantine_confirmed=True,
    )
    callback.message.edit_text.assert_awaited_once_with("✅ Заметка об участнике Иван Иванов сохранена.")
    assert (-1001, 100) not in mentorship._pending_notes


@pytest.mark.asyncio
async def test_note_confirm_service_error_shows_message_without_crashing(monkeypatch):
    import handlers.mentorship as mentorship

    mentorship._pending_notes[(-1001, 100)] = mentorship._PendingNote(
        mentor_account_id="11111111-1111-1111-1111-111111111111",
        stream_id="S1",
        participant_account_id="22222222-2222-2222-2222-222222222222",
        participant_name="Иван Иванов",
        body="переслал скриншот",
        created_at=time.time(),
    )
    add_note_mock = AsyncMock(side_effect=MentorshipServiceError("access_denied", "вызывающий не наставник"))
    monkeypatch.setattr(mentorship.mentorship_service, "add_participant_note", add_note_mock)

    callback = _make_callback("mentor_note:confirm", chat_id=-1001, from_user_id=100)
    await mentorship.cb_mentor_note(callback)

    callback.message.edit_text.assert_awaited_once()
    assert "не удалось" in callback.message.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_note_confirm_without_pending_shows_alert():
    import handlers.mentorship as mentorship

    callback = _make_callback("mentor_note:confirm", chat_id=-1001, from_user_id=999)
    await mentorship.cb_mentor_note(callback)

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("show_alert") is True
