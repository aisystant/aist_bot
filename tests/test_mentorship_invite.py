"""
WP-578 — /mentor_invite (обнаружение согласия, способ 2 из 3, решение
пилота 17.09): наставник отвечает на сообщение участника, бот сам пишет
этому участнику в личку запрос согласия.

F9: usage errors go to the mentor's DM, not the group (silence for an outsider
is covered by test_mentorship_silence.py).
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

from tests.mentorship_helpers import as_stream_reader, assert_only_dm_to_mentor

MENTOR_TG_ID = 100


def _make_message(*, reply_to_message=None, from_user_id=MENTOR_TG_ID):
    from aiogram.types import Message, User, Chat

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = from_user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = -1001
    msg.chat.type = "group"
    msg.reply_to_message = reply_to_message
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
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
async def test_invite_without_reply_target_tells_mentor_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")])
    message = _make_message(reply_to_message=None)

    await mentorship.cmd_mentor_invite(message)

    assert_only_dm_to_mentor(message, "Ответь этой командой на сообщение участника")


@pytest.mark.asyncio
async def test_invite_self_is_rejected_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")])
    target = _make_target_user(MENTOR_TG_ID)  # тот же id, что и у наставника
    message = _make_message(reply_to_message=target)

    await mentorship.cmd_mentor_invite(message)

    assert_only_dm_to_mentor(message, "Нельзя пригласить самого себя.")


@pytest.mark.asyncio
async def test_invite_reader_of_other_stream_is_told_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S2", "mentor")])
    message = _make_message(reply_to_message=_make_target_user(200))

    await mentorship.cmd_mentor_invite(message)

    assert_only_dm_to_mentor(message, "не числишься наставником или пилотом потока S1")


@pytest.mark.asyncio
async def test_invite_participant_without_account_is_told_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", None))
    message = _make_message(reply_to_message=_make_target_user(200))

    await mentorship.cmd_mentor_invite(message)

    assert_only_dm_to_mentor(message, "У участника нет привязанного аккаунта платформы")


@pytest.mark.asyncio
async def test_invite_success_sends_dm_and_confirms(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", "participant-account"))
    message = _make_message(reply_to_message=_make_target_user(200, full_name="Иван Иванов"))

    await mentorship.cmd_mentor_invite(message)

    message.bot.send_message.assert_awaited_once()
    assert message.bot.send_message.await_args.args[0] == 200
    message.reply.assert_awaited_once_with("Приглашение отправлено участнику Иван Иванов в личку.")


@pytest.mark.asyncio
async def test_invite_forbidden_participant_is_reported_to_mentor_privately(monkeypatch):
    import handlers.mentorship as mentorship
    from aiogram.exceptions import TelegramForbiddenError

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", "participant-account"))
    message = _make_message(reply_to_message=_make_target_user(200, full_name="Иван Иванов"))

    async def _send(chat_id, text, **kwargs):
        if chat_id == 200:
            raise TelegramForbiddenError(method=MagicMock(), message="bot can't initiate conversation")

    message.bot.send_message = AsyncMock(side_effect=_send)

    await mentorship.cmd_mentor_invite(message)

    message.reply.assert_not_awaited()
    assert [call.args[0] for call in message.bot.send_message.await_args_list] == [200, MENTOR_TG_ID]
    dm_text = message.bot.send_message.await_args_list[1].args[1]
    assert "Иван Иванов" in dm_text
    assert "не разрешает боту заговорить первым" in dm_text
