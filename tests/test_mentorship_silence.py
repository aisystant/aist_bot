"""
WP-578 F9 - the group mentorship commands (/mentor_stream, /mentor_invite,
/mentor_note, /mentor_card) stay silent for anyone who is not a stream reader,
and tell real readers about usage errors in a DM, never in the group.
"""

import logging
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

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from tests.mentorship_helpers import MENTOR_TG_ID, as_stream_reader, assert_only_dm_to_mentor, make_group_message

REAL_CODE = "S1-2026.3-T"
OTHER_CODE = "S2-2026.4-T"


def _command(args):
    command = MagicMock()
    command.args = args
    return command


_COMMANDS = {
    "stream": lambda mentorship, message: mentorship.cmd_mentor_stream(message, _command(None)),
    "invite": lambda mentorship, message: mentorship.cmd_mentor_invite(message),
    "note": lambda mentorship, message: mentorship.cmd_mentor_note(message, _command(None)),
    "card": lambda mentorship, message: mentorship.cmd_mentor_card(message),
}

# (linked account or None, what list_reader_streams yields or raises)
_OUTSIDERS = [
    pytest.param(None, [], id="no-linked-account"),
    pytest.param("account", [], id="not-a-reader"),
    pytest.param("account", RuntimeError("module disabled"), id="module-disabled"),
    pytest.param("account", OSError("database unreachable"), id="database-failure"),
]


def _patch_guard(monkeypatch, mentorship, *, account, listing):
    list_mock = AsyncMock(side_effect=listing) if isinstance(listing, Exception) else AsyncMock(return_value=listing)
    mocks = {
        "resolve_ory_id_from_chat": AsyncMock(return_value=account),
        "list_reader_streams": list_mock,
        "lookup_stream_chat": AsyncMock(),
        "register_stream_chat": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(mentorship, name, mock)
    return mocks


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", list(_COMMANDS))
@pytest.mark.parametrize("account, listing", _OUTSIDERS)
async def test_outsider_gets_no_reply_and_no_dm(monkeypatch, command_name, account, listing):
    import handlers.mentorship as mentorship

    mocks = _patch_guard(monkeypatch, mentorship, account=account, listing=listing)
    message = make_group_message()

    await _COMMANDS[command_name](mentorship, message)

    message.reply.assert_not_awaited()
    message.answer.assert_not_awaited()
    message.bot.send_message.assert_not_awaited()
    # the guard runs before anything else touches the group registry or the target participant
    mocks["resolve_ory_id_from_chat"].assert_awaited_once()
    mocks["lookup_stream_chat"].assert_not_awaited()
    mocks["register_stream_chat"].assert_not_awaited()


@pytest.mark.asyncio
async def test_module_disabled_is_logged_as_warning(monkeypatch, caplog):
    import handlers.mentorship as mentorship

    _patch_guard(monkeypatch, mentorship, account="account", listing=RuntimeError("module disabled"))

    with caplog.at_level(logging.WARNING, logger="handlers.mentorship"):
        await _COMMANDS["stream"](mentorship, make_group_message())

    assert any("модуль отключён" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_database_failure_is_logged_as_warning_without_details(monkeypatch, caplog):
    import handlers.mentorship as mentorship

    _patch_guard(monkeypatch, mentorship, account="account", listing=OSError("postgres://user:secret@host/db"))

    with caplog.at_level(logging.WARNING, logger="handlers.mentorship"):
        await _COMMANDS["stream"](mentorship, make_group_message())

    messages = [record.getMessage() for record in caplog.records]
    assert any("проверка наставника не удалась: OSError" in text for text in messages)
    assert not any("secret" in text for text in messages)


@pytest.mark.asyncio
async def test_plain_outsider_leaves_no_warning(monkeypatch, caplog):
    import handlers.mentorship as mentorship

    _patch_guard(monkeypatch, mentorship, account="account", listing=[])

    with caplog.at_level(logging.WARNING, logger="handlers.mentorship"):
        await _COMMANDS["stream"](mentorship, make_group_message())

    assert caplog.records == []


@pytest.mark.asyncio
async def test_stream_without_argument_hints_real_codes_in_dm(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[(REAL_CODE, "mentor"), (OTHER_CODE, "pilot")])
    message = make_group_message()

    await mentorship.cmd_mentor_stream(message, _command(None))

    assert_only_dm_to_mentor(message, REAL_CODE)
    hint = message.bot.send_message.await_args.args[1]
    assert OTHER_CODE in hint
    assert "Укажи поток: /mentor_stream S1" not in hint


@pytest.mark.asyncio
async def test_stream_of_someone_else_is_rejected_in_dm(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[(REAL_CODE, "mentor")])
    register = AsyncMock()
    monkeypatch.setattr(mentorship, "register_stream_chat", register)
    message = make_group_message()

    await mentorship.cmd_mentor_stream(message, _command("S9-2099.1-X"))

    assert_only_dm_to_mentor(message, "не числитесь наставником или пилотом потока S9-2099.1-X")
    assert REAL_CODE in message.bot.send_message.await_args.args[1]
    register.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_registers_and_confirms_in_group(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[(REAL_CODE, "mentor")])
    register = AsyncMock(return_value="registered")
    monkeypatch.setattr(mentorship, "register_stream_chat", register)
    message = make_group_message()

    await mentorship.cmd_mentor_stream(message, _command(REAL_CODE.lower()))

    register.assert_awaited_once_with(message.chat.id, REAL_CODE, "mentor-account")
    message.reply.assert_awaited_once_with(f"✅ Группа зарегистрирована за потоком {REAL_CODE}.")
    message.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_reader_lost_between_checks_is_told_in_dm(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[(REAL_CODE, "mentor")])
    monkeypatch.setattr(mentorship, "register_stream_chat", AsyncMock(return_value="not_stream_reader"))
    message = make_group_message()

    await mentorship.cmd_mentor_stream(message, _command(REAL_CODE))

    assert_only_dm_to_mentor(message, f"вы не числитесь наставником или пилотом потока {REAL_CODE}")


def _forbidden():
    return TelegramForbiddenError(method=MagicMock(), message="bot can't initiate conversation")


def _bad_request():
    return TelegramBadRequest(method=MagicMock(), message="chat not found")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure, error_name",
    [
        pytest.param(_forbidden(), "TelegramForbiddenError", id="dm-never-opened"),
        pytest.param(_bad_request(), "TelegramBadRequest", id="other-telegram-error"),
    ],
)
async def test_undeliverable_dm_to_mentor_is_swallowed_and_logged(monkeypatch, caplog, failure, error_name):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[(REAL_CODE, "mentor")])
    message = make_group_message()
    message.bot.send_message = AsyncMock(side_effect=failure)

    with caplog.at_level(logging.WARNING, logger="handlers.mentorship"):
        await mentorship.cmd_mentor_stream(message, _command(None))

    message.reply.assert_not_awaited()
    assert any(f"не доставлено: {error_name}" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", ["invite", "note", "card"])
async def test_unregistered_group_is_told_privately(monkeypatch, command_name):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")])
    monkeypatch.setattr(mentorship, "lookup_stream_chat", AsyncMock(return_value=None))
    message = make_group_message()

    await _COMMANDS[command_name](mentorship, message)

    assert_only_dm_to_mentor(message, "Эта группа не зарегистрирована за потоком")


@pytest.mark.asyncio
async def test_note_without_any_text_is_told_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")])
    message = make_group_message()
    message.reply_to_message.text = None  # e.g. a photo without caption

    await _COMMANDS["note"](mentorship, message)

    assert_only_dm_to_mentor(message, "нечего сохранять")


@pytest.mark.asyncio
async def test_note_participant_without_account_is_told_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", None))
    message = make_group_message()

    await _COMMANDS["note"](mentorship, message)

    assert_only_dm_to_mentor(message, "У участника нет привязанного аккаунта платформы")
    assert (message.chat.id, message.from_user.id) not in mentorship._pending_notes


@pytest.mark.asyncio
async def test_invite_closed_dm_on_both_sides_stays_silent_in_group(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", "participant-account"))
    message = make_group_message()
    message.bot.send_message = AsyncMock(side_effect=_forbidden())

    await mentorship.cmd_mentor_invite(message)

    message.reply.assert_not_awaited()
    assert [call.args[0] for call in message.bot.send_message.await_args_list] == [200, MENTOR_TG_ID]


@pytest.mark.asyncio
async def test_card_undeliverable_dm_gives_no_false_group_confirmation(monkeypatch):
    """WP-578, найдено ревью 25.09: карточка получена от сервиса, но личка
    наставнику не открыта — раньше группа всё равно видела "отправлено тебе
    в личку", хотя карточка (с приватными данными) не дошла никуда."""
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", "participant-account"))
    monkeypatch.setattr(
        mentorship.mentorship_service,
        "get_participant_card",
        AsyncMock(return_value={"manualMinimum": {}, "correspondenceEmpty": True, "recentNotes": []}),
    )
    message = make_group_message()
    message.bot.send_message = AsyncMock(side_effect=_forbidden())

    await mentorship.cmd_mentor_card(message)

    message.reply.assert_not_awaited()
    message.bot.send_message.assert_awaited_once()
