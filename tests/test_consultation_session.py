"""Unit-тесты РП-498 Ф17: память диалога консультации.

Покрывает states.common.consultation: окно неактивности 15 минут, явное сохранение после
очистки по таймауту, причина каждой очистки истории в логе, размер истории в строке enter().
До Ф17 тестов на историю консультации в боте не было вообще.
"""

import logging
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from states.common.consultation import SESSION_TIMEOUT_SEC, ConsultationState  # noqa: E402

LOGGER_NAME = "states.common.consultation"


def make_state() -> ConsultationState:
    return ConsultationState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())


def make_user(chat_id: int, ctx: dict) -> dict:
    return {"chat_id": chat_id, "current_context": ctx, "language": "ru"}


# =============================================================================
# Окно неактивности: 15 минут
# =============================================================================

def test_window_is_fifteen_minutes_and_matches_routing_table():
    from config.settings import SM_EXPECTING_REPLY_STATES

    assert SESSION_TIMEOUT_SEC == 900
    assert SM_EXPECTING_REPLY_STATES["common.consultation"] == 15


@pytest.mark.asyncio
async def test_followup_inside_window_keeps_history():
    state = make_state()
    history = [{"q": "опиши роль", "a": "Ответ. Хочешь, опишем, что делает эта роль?"}]
    ctx = {"consultation_history": history, "consultation_last_activity": time.time() - 14 * 60}
    user = make_user(1001, ctx)

    with patch.object(state, "enter", new=AsyncMock(return_value=None)) as enter, \
         patch.object(state, "_save_session_context", new=AsyncMock()) as save:
        event = await state.handle(user, SimpleNamespace(text="опиши"))

    assert event == "followup"
    enter.assert_awaited_once_with(user, context={"question": "опиши"})
    assert ctx["consultation_history"] == history  # 14 минут: история жива
    save.assert_not_awaited()                      # очистки не было, лишних записей нет


@pytest.mark.asyncio
async def test_followup_after_window_clears_history_and_saves_it_explicitly():
    state = make_state()
    ctx = {
        "consultation_history": [{"q": "старый вопрос", "a": "старый ответ"}],
        "consultation_last_activity": time.time() - 16 * 60,
        "force_role": "navigator",
    }
    user = make_user(1002, ctx)

    with patch.object(state, "enter", new=AsyncMock(return_value=None)) as enter, \
         patch.object(state, "_save_session_context", new=AsyncMock()) as save:
        event = await state.handle(user, SimpleNamespace(text="новая тема разговора"))

    assert event == "followup"
    enter.assert_awaited_once_with(user, context={"question": "новая тема разговора"})
    assert "consultation_history" not in ctx
    assert ctx["force_role"] == "navigator"  # роль из /navigator переживает таймаут
    save.assert_awaited_once()               # очистка сохранена явно, не только в общем объекте
    saved_chat_id, saved_ctx = save.await_args.args
    assert saved_chat_id == 1002
    assert "consultation_history" not in saved_ctx
    assert saved_ctx["force_role"] == "navigator"


# =============================================================================
# Причина очистки истории в логе
# =============================================================================

def test_clear_session_logs_reason_and_pairs(caplog):
    state = make_state()
    ctx = {
        "consultation_history": [{"q": "1", "a": "1"}, {"q": "2", "a": "2"}],
        "consultation_last_activity": 1.0,
        "qa_comment_id": 7,
        "active_free_role": "navigator",
    }

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        state._clear_session(ctx, reason="exit")

    assert "session cleared: reason=exit pairs=2" in caplog.text
    assert ctx == {}  # история, отметка активности, флаг замечания и роль сняты


def test_clear_session_is_silent_for_empty_session(caplog):
    state = make_state()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        state._clear_session({}, reason="exit")

    assert "session cleared" not in caplog.text


@pytest.mark.asyncio
async def test_exit_saves_cleared_context_and_logs_exit_reason(caplog):
    state = make_state()
    ctx = {"consultation_history": [{"q": "q", "a": "a"}], "consultation_last_activity": time.time()}
    user = make_user(1003, ctx)

    with patch.object(state, "_save_session_context", new=AsyncMock()) as save, \
         caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = await state.exit(user)

    assert result == {"consultation_complete": True}
    assert "session cleared: reason=exit pairs=1" in caplog.text
    saved_ctx = save.await_args.args[1]
    assert "consultation_history" not in saved_ctx


@pytest.mark.asyncio
async def test_end_session_logs_end_session_reason(caplog):
    state = make_state()
    ctx = {"consultation_history": [{"q": "q", "a": "a"}], "consultation_last_activity": time.time()}
    user = make_user(1004, ctx)

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock()), \
         caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await state._end_session(user, ctx, "ru")

    assert "session cleared: reason=end_session pairs=1" in caplog.text


# =============================================================================
# Строка enter(): размер истории
# =============================================================================

@pytest.mark.asyncio
async def test_enter_log_line_reports_history_size(caplog):
    state = make_state()
    history = [{"q": "abc", "a": "defgh"}]  # 8 символов
    user = make_user(1005, {"consultation_history": history})

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock()), \
         patch("core.access.access_layer.has_access", new=AsyncMock(return_value=True)), \
         caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await state.enter(user, context={"comment_mode": True, "comment_qa_id": 7})

    assert "history_pairs=1" in caplog.text
    assert "history_chars=8" in caplog.text
