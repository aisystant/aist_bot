"""WP-612: команды удаления данных не оставляют номер человека в журналах."""

import asyncio
import logging
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import pytest
from aiogram.types import CallbackQuery, Message

from core.erasure_trace import (
    ERASURE_TRACE_COMMANDS,
    OMITTED_USER_ID,
    REDACTED_POINT,
    bot_count_sql,
    canonical_trace_command,
    erasure_command_list,
    health_count_sql,
    is_erasure_trace_command,
    learning_count_sql,
    redact_command_list,
    redact_entry_point,
    sensitive_match_sql,
)
from core.tracing import Trace, _dual_write_trace, _save_trace_to_db, finish_trace
from handlers.chat_analyst import (
    ERASE_CANCEL,
    ERASE_CONFIRM,
    RESUME_CANCEL,
    RESUME_CONFIRM,
)

PERSON = 555
ACTIONS = [
    "/erase_me",
    "/resume_me",
    f"cb:{ERASE_CONFIRM}",
    f"cb:{ERASE_CANCEL}",
    f"cb:{RESUME_CONFIRM}",
    f"cb:{RESUME_CANCEL}",
]


def test_six_actions_match_the_handler_buttons():
    assert f"cb:{ERASE_CONFIRM}" in ERASURE_TRACE_COMMANDS
    assert f"cb:{ERASE_CANCEL}" in ERASURE_TRACE_COMMANDS
    assert f"cb:{RESUME_CONFIRM}" in ERASURE_TRACE_COMMANDS
    assert f"cb:{RESUME_CANCEL}" in ERASURE_TRACE_COMMANDS
    assert erasure_command_list() == sorted(ERASURE_TRACE_COMMANDS)
    assert len(ERASURE_TRACE_COMMANDS) == 6


@pytest.mark.parametrize("command", ACTIONS)
def test_erasure_commands_are_recognized(command):
    assert is_erasure_trace_command(command)


@pytest.mark.parametrize("command", ["/start", "/erase_me_extra", "msg:text", "cb:menu:open", ""])
def test_other_commands_keep_the_person(command):
    assert not is_erasure_trace_command(command)


def test_bot_suffix_counts_as_the_same_command():
    assert canonical_trace_command("/erase_me@aist_me_bot") == "/erase_me"
    assert is_erasure_trace_command("/resume_me@aist_me_bot please")


def test_session_list_drops_only_erasure_commands():
    kept = redact_command_list(["/start", "/erase_me", "cb:chat_analyst:resume:confirm", "/learn"])
    assert kept == ["/start", "/learn"]
    assert redact_entry_point("/erase_me") == REDACTED_POINT
    assert redact_entry_point("/start") == "/start"
    assert redact_entry_point(None) is None


def test_count_sql_passes_commands_as_a_parameter():
    sql = "\n".join([
        *health_count_sql().values(),
        *bot_count_sql().values(),
        learning_count_sql()["domain_event.request_traced"],
    ])
    assert "$1::text[]" in sql
    assert str(PERSON) not in sql
    with pytest.raises(ValueError):
        sensitive_match_sql("command; DROP TABLE request_traces")


def _message(text: str):
    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock()
    msg.from_user.id = PERSON
    msg.from_user.username = None
    msg.chat = MagicMock()
    msg.chat.id = PERSON
    msg.text = text
    msg.bot = MagicMock()
    msg.bot.send_chat_action = AsyncMock()
    return msg


def _callback(data: str):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = MagicMock()
    cb.from_user.id = PERSON
    cb.data = data
    return cb


async def _settle():
    await asyncio.sleep(0)
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
    if pending:
        await asyncio.gather(*pending)


async def _run_trace(event):
    from core.middleware import TracingMiddleware

    finished = {}

    async def capture(trace):
        finished["trace"] = trace

    session = AsyncMock()
    touch = AsyncMock()
    langfuse = MagicMock()
    with patch("core.middleware.finish_trace", capture), \
         patch("core.langfuse_client.langfuse_trace", langfuse), \
         patch("core.langfuse_client.langfuse_end_trace", MagicMock()), \
         patch("db.queries.sessions.get_or_create_session", session), \
         patch("db.queries.activity.touch_last_active_date", touch):
        await TracingMiddleware()(AsyncMock(return_value="ok"), event, {})
        await _settle()
    return finished["trace"], langfuse, session, touch


@pytest.mark.parametrize("command", ACTIONS)
async def test_trace_sinks_omit_the_person_and_keep_activity(command):
    event = _callback(command.removeprefix("cb:")) if command.startswith("cb:") else _message(command)
    trace, langfuse, session, touch = await _run_trace(event)

    assert trace.user_id == OMITTED_USER_ID
    assert trace.command == command
    assert langfuse.call_args.kwargs["user_id"] == OMITTED_USER_ID
    assert langfuse.call_args.kwargs["name"] == command
    session.assert_not_called()
    touch.assert_awaited_once_with(PERSON)


async def test_ordinary_command_still_records_the_person():
    trace, langfuse, session, touch = await _run_trace(_message("/start"))

    assert trace.user_id == PERSON
    assert langfuse.call_args.kwargs["user_id"] == PERSON
    session.assert_awaited_once_with(PERSON, "/start")
    touch.assert_awaited_once_with(PERSON)


async def test_trace_log_and_platform_event_keep_the_action_without_the_person(caplog):
    trace = Trace(
        trace_id="abc",
        user_id=PERSON,
        command="/erase_me",
        state="private",
        start=time.perf_counter(),
    )
    resolve = AsyncMock()
    post = AsyncMock()
    with caplog.at_level(logging.INFO, logger="core.tracing"), \
         patch("core.tracing._save_trace_to_db", new=AsyncMock()), \
         patch("core.tracing._dual_write_trace", new=AsyncMock()):
        await finish_trace(trace)
    assert "user=omitted" in caplog.text
    assert str(PERSON) not in caplog.text
    assert "/erase_me" in caplog.text

    with patch("helpers.dual_write.resolve_ory_id_from_chat", resolve), \
         patch("helpers.dual_write.post_event", post):
        await _dual_write_trace(trace)
    resolve.assert_not_called()
    payload = post.await_args.kwargs["payload"]
    assert post.await_args.kwargs["account_id"] is None
    assert payload["command"] == "/erase_me"
    assert payload["state"] == "private"
    assert "total_ms" in payload
    assert str(PERSON) not in str(payload)


async def test_saved_trace_uses_zero_even_if_the_object_still_has_the_person():
    trace = Trace(
        trace_id="abc",
        user_id=PERSON,
        command="cb:chat_analyst:erase:confirm",
        state="private",
        start=time.perf_counter(),
    )
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = False
    with patch("db.connection.get_health_pool", new=AsyncMock(return_value=pool)):
        await _save_trace_to_db(trace)
    args = conn.execute.await_args.args
    assert args[2] == OMITTED_USER_ID
    assert args[3] == "cb:chat_analyst:erase:confirm"


async def test_ordinary_platform_event_still_carries_the_account():
    trace = Trace(
        trace_id="abc",
        user_id=PERSON,
        command="/start",
        state="private",
        start=time.perf_counter(),
    )
    resolve = AsyncMock(return_value="ory-1")
    post = AsyncMock()
    with patch("helpers.dual_write.resolve_ory_id_from_chat", resolve), \
         patch("helpers.dual_write.post_event", post):
        await _dual_write_trace(trace)
    resolve.assert_awaited_once_with(PERSON)
    assert post.await_args.kwargs["account_id"] == "ory-1"


async def test_session_writer_ignores_erasure_before_opening_the_database():
    from db.queries.sessions import get_or_create_session

    with patch("db.queries.sessions.get_health_pool", new=AsyncMock()) as pool:
        await get_or_create_session(PERSON, "/resume_me")
        await get_or_create_session(PERSON, "cb:chat_analyst:resume:cancel")
    pool.assert_not_called()


async def test_log_line_omits_the_person_only_for_erasure(caplog):
    from core.middleware import LoggingMiddleware

    with patch("db.queries.users.clear_bot_blocked", new=AsyncMock()), \
         caplog.at_level(logging.INFO, logger="core.middleware"):
        await LoggingMiddleware()(AsyncMock(), _message("/erase_me"), {})
        await LoggingMiddleware()(AsyncMock(), _message("/start"), {})
        await _settle()
    erasure, ordinary = [line for line in caplog.text.splitlines() if "Получено сообщение" in line]
    assert "user_id=omitted" in erasure
    assert str(PERSON) not in erasure
    assert f"user_id={PERSON}" in ordinary


def test_langfuse_replaces_the_person_for_an_erasure_command():
    import sys
    from types import ModuleType

    lf = MagicMock()
    lf.create_trace_id.return_value = "ab" * 16
    ctx = MagicMock()
    fake = ModuleType("langfuse")
    fake.propagate_attributes = MagicMock(return_value=ctx)
    fake_types = ModuleType("langfuse.types")
    fake_types.TraceContext = MagicMock(return_value="ctx")
    with patch.dict(sys.modules, {"langfuse": fake, "langfuse.types": fake_types}), \
         patch("core.langfuse_client._get_langfuse", return_value=lf):
        from core.langfuse_client import langfuse_trace
        langfuse_trace(user_id=PERSON, name="/resume_me", trace_id="abc", session_id="real-session")
    kwargs = fake.propagate_attributes.call_args.kwargs
    assert kwargs["user_id"] == "0"
    assert kwargs["session_id"] == "0"
    assert kwargs["trace_name"] == "/resume_me"
    assert str(PERSON) not in str(kwargs)
