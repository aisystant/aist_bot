"""
Smoke-тесты: WP-7 TGSH11 — Session heartbeat E2E smoke.

Критерии приёмки:
- (a) session.heartbeat → edit_message_text с обновлённым счётчиком + send_chat_action(typing)
- (b) session.turn_failed → _heartbeat_soft_fail → edit «❌ Ход не завершён»
- (c) нет heartbeat 15+ мин → auto-fail «❌ Ход не завершён»
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

from tests.smoke.conftest import ThinMockBot
from handlers import external_session as ext_sess


@pytest.fixture
def mock_bot():
    return ThinMockBot()


def _make_mock_pool(fetchrow_return):
    """Создаёт mock asyncpg pool с заданным return_value для fetchrow."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=fetchrow_return)

    mock_pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    mock_pool.acquire = _acquire
    return mock_pool


# ─── TGSH11(a): heartbeat counter + typing indicator ───

@pytest.mark.asyncio
async def test_heartbeat_counter_updates(mock_bot):
    """session.heartbeat → edit_message_text с счётчиком + send_chat_action(typing)."""
    chat_id = 12345
    session_id = "test-session-001"
    working_message_id = 42

    heartbeat_row = {
        "event_type": "session.heartbeat",
        "occurred_at": datetime.now(timezone.utc),
        "payload": {"session_id": session_id, "turn_n": 1, "elapsed_sec": 12, "progress_hint": "thinking"},
    }
    mock_pool = _make_mock_pool(heartbeat_row)

    with patch.object(ext_sess, "_HEARTBEAT_POLL_INTERVAL_SEC", 0.01), \
         patch.object(ext_sess, "_HEARTBEAT_TIMEOUT_SEC", 300), \
         patch("db.connection.get_learning_pool", new_callable=AsyncMock, return_value=mock_pool):

        await ext_sess._start_heartbeat_poller(mock_bot, chat_id, session_id, working_message_id, turn_n=1)
        await asyncio.sleep(0.05)
        await ext_sess._stop_heartbeat_poller(chat_id)

    edits = mock_bot.get_sent("edit_message_text")
    assert len(edits) >= 1
    assert "⏳ Работаю..." in edits[0]["text"]
    assert "12с" in edits[0]["text"]
    assert "thinking" in edits[0]["text"]

    actions = mock_bot.get_sent("send_chat_action")
    assert len(actions) >= 1
    assert actions[0]["action"] == "typing"


# ─── TGSH11(b): turn_failed → fail-message ───

@pytest.mark.asyncio
async def test_turn_failed_sends_fail_message(mock_bot):
    """session.turn_failed → _heartbeat_soft_fail → edit «❌ Ход не завершён»."""
    chat_id = 12345
    session_id = "test-session-002"
    working_message_id = 43

    turn_failed_row = {
        "event_type": "session.turn_failed",
        "occurred_at": datetime.now(timezone.utc),
        "payload": {"session_id": session_id, "turn_n": 1, "reason": "unknown"},
    }
    mock_pool = _make_mock_pool(turn_failed_row)

    with patch.object(ext_sess, "_HEARTBEAT_POLL_INTERVAL_SEC", 0.01), \
         patch.object(ext_sess, "_HEARTBEAT_TIMEOUT_SEC", 300), \
         patch("db.connection.get_learning_pool", new_callable=AsyncMock, return_value=mock_pool):

        await ext_sess._start_heartbeat_poller(mock_bot, chat_id, session_id, working_message_id, turn_n=1)
        await asyncio.sleep(0.05)
        await ext_sess._stop_heartbeat_poller(chat_id)

    edits = mock_bot.get_sent("edit_message_text")
    assert len(edits) >= 1
    assert "❌ Ход не завершён" in edits[-1]["text"]
    assert "обработчик завершился с ошибкой" in edits[-1]["text"]


# ─── TGSH11(c): no heartbeat → auto-fail ───

@pytest.mark.asyncio
async def test_no_heartbeat_auto_fail(mock_bot):
    """Нет heartbeat 15+ мин → auto-fail с «❌ Ход не завершён»."""
    chat_id = 12345
    session_id = "test-session-003"
    working_message_id = 44

    # fetchrow всегда возвращает None → no_heartbeat path
    mock_pool = _make_mock_pool(None)

    with patch.object(ext_sess, "_HEARTBEAT_POLL_INTERVAL_SEC", 0.01), \
         patch.object(ext_sess, "_HEARTBEAT_TIMEOUT_SEC", 0.05), \
         patch("db.connection.get_learning_pool", new_callable=AsyncMock, return_value=mock_pool):

        await ext_sess._start_heartbeat_poller(mock_bot, chat_id, session_id, working_message_id, turn_n=1)
        # Даём поллеру время проснуться, проверить timeout и выйти
        await asyncio.sleep(0.12)
        await ext_sess._stop_heartbeat_poller(chat_id)

    edits = mock_bot.get_sent("edit_message_text")
    assert len(edits) >= 1
    assert "❌ Ход не завершён" in edits[-1]["text"]
    assert "не начал отвечать" in edits[-1]["text"]


# ─── TGSH11: stale heartbeat → auto-fail ───

@pytest.mark.asyncio
async def test_stale_heartbeat_auto_fail(mock_bot):
    """Heartbeat был, но затем замолчал ≥15 мин → auto-fail."""
    chat_id = 12345
    session_id = "test-session-004"
    working_message_id = 45

    # Первый вызов — heartbeat, второй — None (stale)
    now = datetime.now(timezone.utc)
    old_heartbeat = now - timedelta(seconds=1000)

    call_count = 0
    async def _fetchrow(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "event_type": "session.heartbeat",
                "occurred_at": old_heartbeat,
                "payload": {"session_id": session_id, "turn_n": 1, "elapsed_sec": 30},
            }
        return None

    mock_conn = MagicMock()
    mock_conn.fetchrow = _fetchrow
    mock_pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    mock_pool.acquire = _acquire

    with patch.object(ext_sess, "_HEARTBEAT_POLL_INTERVAL_SEC", 0.01), \
         patch.object(ext_sess, "_HEARTBEAT_TIMEOUT_SEC", 60), \
         patch("db.connection.get_learning_pool", new_callable=AsyncMock, return_value=mock_pool):

        await ext_sess._start_heartbeat_poller(mock_bot, chat_id, session_id, working_message_id, turn_n=1)
        await asyncio.sleep(0.08)
        await ext_sess._stop_heartbeat_poller(chat_id)

    edits = mock_bot.get_sent("edit_message_text")
    # Первый edit — счётчик, второй — fail
    assert len(edits) >= 2
    assert "❌ Ход не завершён" in edits[-1]["text"]
    assert "замолчал" in edits[-1]["text"]
