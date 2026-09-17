"""
WP-578 — /mentor_invite (обнаружение согласия, способ 2 из 3, решение
пилота 17.09): наставник отвечает на сообщение участника, бот сам пишет
этому участнику в личку запрос согласия.
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


def _make_message(*, reply_to_message=None, from_user_id=100):
    from aiogram.types import Message, User, Chat

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = from_user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = -1001
    msg.chat.type = "group"
    msg.reply_to_message = reply_to_message
    msg.reply = AsyncMock()
    msg.bot = AsyncMock()
    return msg


def _make_target_user(user_id: int, full_name: str = "Участник Тестов"):
    from aiogram.types import Message, User

    target = MagicMock(spec=Message)
    target.from_user = MagicMock(spec=User)
    target.from_user.id = user_id
    target.from_user.full_name = full_name
    return target


@pytest.mark.asyncio
async def test_invite_without_reply_target_asks_to_reply():
    import handlers.mentorship as mentorship

    message = _make_message(reply_to_message=None)
    await mentorship.cmd_mentor_invite(message)

    message.reply.assert_awaited_once()
    assert "ответь" in message.reply.await_args.args[0].lower()
    message.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_invite_self_is_rejected():
    import handlers.mentorship as mentorship

    target = _make_target_user(100)  # тот же id, что и у наставника
    message = _make_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_invite(message)

    message.reply.assert_awaited_once_with("Нельзя пригласить самого себя.")
    message.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_invite_rejects_non_stream_reader(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value="mentor-account"))
    monkeypatch.setattr(
        mentorship,
        "lookup_stream_chat",
        AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id="mentor-account")),
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value=None))

    target = _make_target_user(200)
    message = _make_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_invite(message)

    assert "не числишься наставником" in message.reply.await_args.args[0]
    message.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_invite_success_sends_dm_and_confirms(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=["mentor-account", "participant-account"]))
    monkeypatch.setattr(
        mentorship,
        "lookup_stream_chat",
        AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id="mentor-account")),
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))

    target = _make_target_user(200, full_name="Иван Иванов")
    message = _make_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_invite(message)

    message.bot.send_message.assert_awaited_once()
    sent_chat_id = message.bot.send_message.await_args.args[0]
    assert sent_chat_id == 200
    message.reply.assert_awaited_once_with("Приглашение отправлено участнику Иван Иванов в личку.")


@pytest.mark.asyncio
async def test_invite_reports_forbidden_error_without_crashing(monkeypatch):
    import handlers.mentorship as mentorship
    from aiogram.exceptions import TelegramForbiddenError

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=["mentor-account", "participant-account"]))
    monkeypatch.setattr(
        mentorship,
        "lookup_stream_chat",
        AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id="mentor-account")),
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))

    target = _make_target_user(200, full_name="Иван Иванов")
    message = _make_message(reply_to_message=target, from_user_id=100)
    message.bot.send_message = AsyncMock(
        side_effect=TelegramForbiddenError(method=MagicMock(), message="bot can't initiate conversation")
    )

    await mentorship.cmd_mentor_invite(message)

    reply_text = message.reply.await_args.args[0]
    assert "Иван Иванов" in reply_text
    assert "не разрешает боту заговорить первым" in reply_text
