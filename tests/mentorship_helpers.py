"""Shared fakes for the WP-578 mentorship command tests (F9 command guard)."""

from unittest.mock import AsyncMock, MagicMock

from db.queries.mentorship import StreamChatContext

MENTOR_TG_ID = 100


def make_group_message(*, from_user_id=MENTOR_TG_ID, target_user_id=200):
    """A group message that replies to a participant's message; reply/send_message are recorders."""
    from aiogram.types import Chat, Message, User

    target = MagicMock(spec=Message)
    target.from_user = MagicMock(spec=User)
    target.from_user.id = target_user_id
    target.from_user.full_name = "Участник Тестов"
    target.text = "вопрос участника"
    target.caption = None

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = from_user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = -1001
    msg.chat.type = "group"
    msg.reply_to_message = target
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.bot = AsyncMock()
    return msg


def as_stream_reader(monkeypatch, mentorship, *, streams, account_ids=("mentor-account",)):
    """Caller is linked to an account and reads the given streams; the group is registered to S1.

    account_ids are returned by consecutive resolve_ory_id_from_chat calls: the caller first,
    then (when the command resolves one) the target participant.
    """
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=list(account_ids)))
    monkeypatch.setattr(mentorship, "list_reader_streams", AsyncMock(return_value=streams))
    monkeypatch.setattr(
        mentorship,
        "lookup_stream_chat",
        AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id=account_ids[0])),
    )


def assert_only_dm_to_mentor(message, expected_fragment: str):
    """Exactly one DM to the mentor with the expected text; nothing in the group."""
    message.reply.assert_not_awaited()
    message.answer.assert_not_awaited()
    message.bot.send_message.assert_awaited_once()
    assert message.bot.send_message.await_args.args[0] == message.from_user.id
    assert expected_fragment in message.bot.send_message.await_args.args[1]
