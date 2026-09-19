"""
WP-578 — /mentor_note в личке с ботом (актуализация 19.09): наставник
пересылает сообщение (участника или своё собственное) в чат с ботом и
отвечает на него командой — тот же поток подтверждения карантина, что у
групповой версии (tests/test_mentorship_note.py), но участник определяется
без группового контекста: по forward_origin пересланного сообщения, либо по
«активному участнику» последнего однозначно определённого forward'а.
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

import pytest
from unittest.mock import AsyncMock, MagicMock

from db.queries.mentorship import StreamChatContext

MENTOR_ID = "11111111-1111-1111-1111-111111111111"
PARTICIPANT_ID = "22222222-2222-2222-2222-222222222222"


def _make_dm_message(*, reply_to_message=None, from_user_id=100, args=None):
    from aiogram.types import Message, User, Chat

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = from_user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = from_user_id  # личка: chat.id совпадает с user id
    msg.chat.type = "private"
    msg.reply_to_message = reply_to_message
    msg.reply = AsyncMock()
    msg.bot = AsyncMock()
    command = MagicMock()
    command.args = args
    return msg, command


def _make_forwarded_from_participant(participant_user_id: int, *, full_name: str = "Иван Иванов", text: str = "вопрос от участника"):
    """Пересланное в личку сообщение, автор оригинала — участник (не наставник)."""
    from aiogram.types import Message, User

    origin_user = MagicMock(spec=User)
    origin_user.id = participant_user_id
    origin_user.full_name = full_name

    origin = MagicMock()
    origin.sender_user = origin_user

    target = MagicMock(spec=Message)
    target.text = text
    target.caption = None
    target.forward_origin = origin
    return target


def _make_forwarded_own_message(mentor_user_id: int, *, text: str = "мой старый ответ участнику"):
    """Пересланное наставником своё же старое сообщение — Телеграм не хранит адресата."""
    from aiogram.types import Message, User

    origin_user = MagicMock(spec=User)
    origin_user.id = mentor_user_id

    origin = MagicMock()
    origin.sender_user = origin_user

    target = MagicMock(spec=Message)
    target.text = text
    target.caption = None
    target.forward_origin = origin
    return target


def _make_plain_reply(text: str = "просто текст, не форвард"):
    """Ответ на обычное (не пересланное) сообщение в личке — forward_origin отсутствует."""
    from aiogram.types import Message

    target = MagicMock(spec=Message)
    target.text = text
    target.caption = None
    target.forward_origin = None
    return target


@pytest.fixture(autouse=True)
def _clear_state():
    import handlers.mentorship as mentorship

    mentorship._pending_notes.clear()
    mentorship._active_participant.clear()
    yield
    mentorship._pending_notes.clear()
    mentorship._active_participant.clear()


@pytest.mark.asyncio
async def test_dm_note_without_reply_target_asks_to_forward():
    import handlers.mentorship as mentorship

    message, command = _make_dm_message(reply_to_message=None)
    await mentorship.cmd_mentor_note_dm(message, command)

    message.reply.assert_awaited_once()
    assert "перешли" in message.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_dm_note_empty_body_rejected():
    import handlers.mentorship as mentorship

    target = _make_forwarded_from_participant(200, text="")
    message, command = _make_dm_message(reply_to_message=target)

    await mentorship.cmd_mentor_note_dm(message, command)

    message.reply.assert_awaited_once_with("В сообщении-цели нет текста — нечего сохранять.")


@pytest.mark.asyncio
async def test_dm_note_caller_without_account_rejected(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=None))
    target = _make_forwarded_from_participant(200)
    message, command = _make_dm_message(reply_to_message=target)

    await mentorship.cmd_mentor_note_dm(message, command)

    message.reply.assert_awaited_once_with("Не нашёл твой аккаунт платформы — сначала привяжи его (/link).")


@pytest.mark.asyncio
async def test_dm_note_forward_from_participant_resolves_and_stages(monkeypatch):
    import handlers.mentorship as mentorship

    # Порядок вызовов resolve_ory_id_from_chat: (1) наставник — проверка
    # аккаунта, (2) внутри резолвера — участник, (3) внутри резолвера —
    # наставник ещё раз для get_stream_reader_role.
    monkeypatch.setattr(
        mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=[MENTOR_ID, PARTICIPANT_ID, MENTOR_ID])
    )
    monkeypatch.setattr(
        mentorship, "lookup_participant_stream", AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id=MENTOR_ID))
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))

    target = _make_forwarded_from_participant(200, full_name="Иван Иванов", text="вопрос от участника")
    message, command = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note_dm(message, command)

    pending = mentorship._pending_notes[(100, 100)]
    assert pending.body == "вопрос от участника"
    assert pending.participant_account_id == PARTICIPANT_ID
    assert pending.stream_id == "S1"
    reply_text = message.reply.await_args.args[0]
    assert "Иван Иванов" in reply_text

    # forward от участника обновляет «активного участника» на будущее
    active = mentorship._active_participant[100]
    assert active.participant_account_id == PARTICIPANT_ID
    assert active.stream_id == "S1"


@pytest.mark.asyncio
async def test_dm_note_forward_from_participant_not_in_mentor_stream_rejected(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(
        mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=[MENTOR_ID, PARTICIPANT_ID, MENTOR_ID])
    )
    monkeypatch.setattr(
        mentorship, "lookup_participant_stream", AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id=MENTOR_ID))
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value=None))

    target = _make_forwarded_from_participant(200)
    message, command = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note_dm(message, command)

    assert "не числишься наставником" in message.reply.await_args.args[0]
    assert (100, 100) not in mentorship._pending_notes


@pytest.mark.asyncio
async def test_dm_note_own_forward_without_active_participant_asks_for_participant_forward_first(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    target = _make_forwarded_own_message(100)
    message, command = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note_dm(message, command)

    reply_text = message.reply.await_args.args[0]
    assert "не могу понять" in reply_text.lower()
    assert (100, 100) not in mentorship._pending_notes


@pytest.mark.asyncio
async def test_dm_note_plain_reply_without_active_participant_asks_for_participant_forward_first(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    target = _make_plain_reply()
    message, command = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note_dm(message, command)

    reply_text = message.reply.await_args.args[0]
    assert "не могу понять" in reply_text.lower()


@pytest.mark.asyncio
async def test_dm_note_own_forward_uses_active_participant(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    monkeypatch.setattr(mentorship, "time", lambda: 5000.0)
    mentorship._active_participant[100] = mentorship._ActiveParticipant(
        stream_id="S1",
        participant_account_id=PARTICIPANT_ID,
        participant_name="Иван Иванов",
        set_at=4900.0,
    )

    target = _make_forwarded_own_message(100, text="мой старый ответ участнику")
    message, command = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note_dm(message, command)

    pending = mentorship._pending_notes[(100, 100)]
    assert pending.body == "мой старый ответ участнику"
    assert pending.participant_account_id == PARTICIPANT_ID
    assert pending.stream_id == "S1"


@pytest.mark.asyncio
async def test_dm_note_active_participant_expired_asks_for_participant_forward_first(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    monkeypatch.setattr(mentorship, "time", lambda: 99999.0)
    mentorship._active_participant[100] = mentorship._ActiveParticipant(
        stream_id="S1",
        participant_account_id=PARTICIPANT_ID,
        participant_name="Иван Иванов",
        set_at=0.0,
    )

    target = _make_forwarded_own_message(100)
    message, command = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note_dm(message, command)

    reply_text = message.reply.await_args.args[0]
    assert "не могу понять" in reply_text.lower()
    assert (100, 100) not in mentorship._pending_notes


@pytest.mark.asyncio
async def test_dm_note_command_arg_overrides_target_text(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(
        mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=[MENTOR_ID, PARTICIPANT_ID, MENTOR_ID])
    )
    monkeypatch.setattr(
        mentorship, "lookup_participant_stream", AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id=MENTOR_ID))
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))

    target = _make_forwarded_from_participant(200, text="исходный текст")
    message, command = _make_dm_message(reply_to_message=target, from_user_id=100, args="явный текст заметки")

    await mentorship.cmd_mentor_note_dm(message, command)

    pending = mentorship._pending_notes[(100, 100)]
    assert pending.body == "явный текст заметки"


@pytest.mark.asyncio
async def test_dm_note_participant_without_account_rejected(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=[MENTOR_ID, None]))
    target = _make_forwarded_from_participant(200)
    message, command = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_note_dm(message, command)

    reply_text = message.reply.await_args.args[0]
    assert "нет привязанного аккаунта" in reply_text
    assert (100, 100) not in mentorship._pending_notes
