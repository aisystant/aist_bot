"""
Smoke-тесты для middleware.

Проверяет что все middleware:
1. Импортируются без ошибок (ловит lazy import в __call__)
2. Инициализируются без ошибок
3. Пропускают событие без краша

Инцидент-триггер: B4.3 добавил RateLimitMiddleware с lazy import
`from config.settings import DEVELOPER_CHAT_ID` внутри __call__.
DEVELOPER_CHAT_ID не существовал → ImportError при каждом сообщении →
aiogram глотал молча → бот не отвечал никому (14 часов).
"""

import json
import sys
import os
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone


def _make_fake_message(user_id: int = 999):
    """Минимальный fake Message для проверки middleware."""
    from aiogram.types import Message, User, Chat
    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = user_id
    msg.from_user.username = "testuser"
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = user_id
    msg.chat.type = "private"
    msg.text = "/start"
    msg.date = datetime.now()
    msg.bot = MagicMock()
    msg.bot.send_chat_action = AsyncMock()
    msg.answer = AsyncMock()
    return msg


async def _noop_handler(event, data):
    return None


class TestMiddlewareImports:
    """Проверяет что middleware импортируются без ошибок."""

    def test_import_rate_limit(self):
        from core.middleware import RateLimitMiddleware
        assert RateLimitMiddleware is not None

    def test_import_maintenance(self):
        from core.middleware import MaintenanceMiddleware
        assert MaintenanceMiddleware is not None

    def test_import_logging(self):
        from core.middleware import LoggingMiddleware
        assert LoggingMiddleware is not None

    def test_import_tracing(self):
        from core.middleware import TracingMiddleware
        assert TracingMiddleware is not None

    def test_import_consultation_passthrough(self):
        from core.middleware import ConsultationPassthroughMiddleware
        assert ConsultationPassthroughMiddleware is not None

    def test_import_update_dedup(self):
        from core.middleware import UpdateDedupMiddleware
        assert UpdateDedupMiddleware is not None

    def test_config_imports(self):
        """Все константы из config.settings, используемые в middleware, существуют."""
        from config.settings import (
            DEVELOPER_CHAT_ID,
            MAINTENANCE_MODE,
            ALLOWED_TESTERS,
            MAINTENANCE_REDIRECT_BOT,
        )
        assert isinstance(DEVELOPER_CHAT_ID, int)
        assert isinstance(MAINTENANCE_MODE, bool)
        assert isinstance(ALLOWED_TESTERS, set)
        assert isinstance(MAINTENANCE_REDIRECT_BOT, str)


class TestMiddlewareInit:
    """Проверяет инициализацию middleware."""

    def test_rate_limit_init(self):
        from core.middleware import RateLimitMiddleware
        mw = RateLimitMiddleware()
        assert mw._max == 20
        assert mw._window == 60

    def test_rate_limit_custom_params(self):
        from core.middleware import RateLimitMiddleware
        mw = RateLimitMiddleware(max_messages=10, window_seconds=30)
        assert mw._max == 10
        assert mw._window == 30

    def test_maintenance_init(self):
        from core.middleware import MaintenanceMiddleware
        mw = MaintenanceMiddleware()
        assert mw is not None

    def test_logging_init(self):
        from core.middleware import LoggingMiddleware
        mw = LoggingMiddleware()
        assert mw is not None


@pytest.mark.asyncio
class TestMiddlewareCall:
    """Проверяет что __call__ не падает с ImportError или другими ошибками."""

    async def test_rate_limit_call_does_not_crash(self):
        """RateLimitMiddleware пропускает сообщение без краша."""
        from core.middleware import RateLimitMiddleware
        mw = RateLimitMiddleware()
        msg = _make_fake_message(user_id=42)
        handler_called = []

        async def handler(event, data):
            handler_called.append(True)

        await mw(handler, msg, {})
        assert handler_called, "Handler должен быть вызван"

    async def test_rate_limit_allows_developer(self):
        """Разработчик (DEVELOPER_CHAT_ID) не ограничивается rate limit."""
        from core.middleware import RateLimitMiddleware
        from config.settings import DEVELOPER_CHAT_ID

        mw = RateLimitMiddleware(max_messages=0, window_seconds=60)  # лимит 0 — никто не пройдёт
        msg = _make_fake_message(user_id=DEVELOPER_CHAT_ID)
        handler_called = []

        async def handler(event, data):
            handler_called.append(True)

        await mw(handler, msg, {})
        assert handler_called, "Разработчик должен проходить даже при лимите 0"

    async def test_maintenance_off_passes_all(self):
        """MaintenanceMiddleware при MAINTENANCE_MODE=false пропускает всех."""
        from core.middleware import MaintenanceMiddleware
        import core.middleware as mw_module

        original = mw_module.MAINTENANCE_MODE
        try:
            mw_module.MAINTENANCE_MODE = False
            mw = MaintenanceMiddleware()
            msg = _make_fake_message(user_id=999)
            handler_called = []

            async def handler(event, data):
                handler_called.append(True)

            await mw(handler, msg, {})
            assert handler_called
        finally:
            mw_module.MAINTENANCE_MODE = original

    async def test_maintenance_on_blocks_non_tester(self):
        """MaintenanceMiddleware при MAINTENANCE_MODE=True блокирует обычных пользователей."""
        from core.middleware import MaintenanceMiddleware
        import core.middleware as mw_module

        original = mw_module.MAINTENANCE_MODE
        try:
            mw_module.MAINTENANCE_MODE = True
            mw = MaintenanceMiddleware()
            msg = _make_fake_message(user_id=99999)  # не тестер
            handler_called = []

            async def handler(event, data):
                handler_called.append(True)

            await mw(handler, msg, {})
            assert not handler_called, "Обычный пользователь не должен проходить при MAINTENANCE_MODE=True"
        finally:
            mw_module.MAINTENANCE_MODE = original

    async def test_maintenance_on_allows_tester(self):
        """MaintenanceMiddleware при MAINTENANCE_MODE=True пропускает ALLOWED_TESTERS."""
        from core.middleware import MaintenanceMiddleware
        import core.middleware as mw_module

        original_mode = mw_module.MAINTENANCE_MODE
        original_testers = mw_module.ALLOWED_TESTERS
        tester_id = 777777
        try:
            mw_module.MAINTENANCE_MODE = True
            mw_module.ALLOWED_TESTERS = {tester_id}
            mw = MaintenanceMiddleware()
            msg = _make_fake_message(user_id=tester_id)
            handler_called = []

            async def handler(event, data):
                handler_called.append(True)

            await mw(handler, msg, {})
            assert handler_called, "Тестер должен проходить при MAINTENANCE_MODE=True"
        finally:
            mw_module.MAINTENANCE_MODE = original_mode
            mw_module.ALLOWED_TESTERS = original_testers

    async def test_rate_limit_blocks_after_limit(self):
        """RateLimitMiddleware блокирует после превышения лимита."""
        from core.middleware import RateLimitMiddleware
        mw = RateLimitMiddleware(max_messages=2, window_seconds=60)
        msg = _make_fake_message(user_id=777)
        call_count = []

        async def handler(event, data):
            call_count.append(True)

        await mw(handler, msg, {})
        await mw(handler, msg, {})
        await mw(handler, msg, {})  # должен быть заблокирован

        assert len(call_count) == 2, "После лимита handler не должен вызываться"

    async def test_update_dedup_call_does_not_crash(self):
        """UpdateDedupMiddleware пропускает событие без краша, если event_update отсутствует в data."""
        from core.middleware import UpdateDedupMiddleware
        mw = UpdateDedupMiddleware()
        msg = _make_fake_message(user_id=42)
        handler_called = []

        async def handler(event, data):
            handler_called.append(True)

        await mw(handler, msg, {})
        assert handler_called, "Без event_update в data — handler должен вызываться (fail-open)"

    async def test_update_dedup_blocks_repeated_update_id(self):
        """Повторная доставка того же update_id (webhook retry) отбрасывается."""
        from core.middleware import UpdateDedupMiddleware
        mw = UpdateDedupMiddleware()
        msg = _make_fake_message(user_id=42)
        fake_update = MagicMock()
        fake_update.update_id = 555
        data = {"event_update": fake_update}
        call_count = []

        async def handler(event, data):
            call_count.append(True)

        await mw(handler, msg, data)
        await mw(handler, msg, data)  # тот же update_id — webhook retry

        assert len(call_count) == 1, "Повторный update_id не должен вызывать handler дважды"

    async def test_update_dedup_allows_different_update_ids(self):
        """Разные update_id не считаются дублями."""
        from core.middleware import UpdateDedupMiddleware
        mw = UpdateDedupMiddleware()
        msg = _make_fake_message(user_id=42)
        call_count = []

        async def handler(event, data):
            call_count.append(True)

        for uid in (1, 2, 3):
            fake_update = MagicMock()
            fake_update.update_id = uid
            await mw(handler, msg, {"event_update": fake_update})

        assert len(call_count) == 3, "Разные update_id должны проходить все"


def _session_pool(conn):
    acquire_context = MagicMock()
    acquire_context.__aenter__ = AsyncMock(return_value=conn)
    acquire_context.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_context
    return pool


@pytest.mark.asyncio
class TestSessionTracking:
    """Сессия измеряет активность между запросами, а не время отсутствия."""

    async def test_request_within_timeout_updates_last_activity(self, monkeypatch):
        import db.queries.sessions as sessions

        now = datetime.now(timezone.utc)
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={
            "id": 7,
            "started_at": now - timedelta(minutes=10),
            "ended_at": now - timedelta(minutes=5),
            "request_count": 2,
            "commands": '["/start"]',
        })
        conn.execute = AsyncMock()
        monkeypatch.setattr(
            sessions, "get_health_pool", AsyncMock(return_value=_session_pool(conn))
        )
        log_event = AsyncMock()
        monkeypatch.setattr(sessions, "log_event", log_event)

        await sessions.get_or_create_session(42, "/learn")

        sql, session_id, ended_at, duration, exit_point, commands = (
            conn.execute.await_args.args
        )
        assert "UPDATE user_sessions" in sql
        assert session_id == 7
        assert 9 * 60 <= duration <= 11 * 60
        assert exit_point == "/learn"
        assert json.loads(commands) == ["/start", "/learn"]
        assert ended_at >= now
        log_event.assert_not_awaited()

    async def test_request_after_timeout_creates_new_closed_interval(self, monkeypatch):
        import db.queries.sessions as sessions

        now = datetime.now(timezone.utc)
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={
            "id": 8,
            "started_at": now - timedelta(hours=3),
            "ended_at": now - timedelta(hours=2),
            "request_count": 4,
            "commands": '["/learn"]',
        })
        conn.execute = AsyncMock()
        monkeypatch.setattr(
            sessions, "get_health_pool", AsyncMock(return_value=_session_pool(conn))
        )
        log_event = AsyncMock()
        monkeypatch.setattr(sessions, "log_event", log_event)

        await sessions.get_or_create_session(42, "/start")

        sql, chat_id, started_at, entry_point, commands = conn.execute.await_args.args
        assert "INSERT INTO user_sessions" in sql
        assert "ended_at, duration_seconds" in sql
        assert chat_id == 42
        assert started_at >= now
        assert entry_point == "/start"
        assert json.loads(commands) == ["/start"]
        log_event.assert_awaited_once()

    async def test_legacy_cleanup_does_not_invent_duration(self, monkeypatch):
        import db.queries.sessions as sessions

        conn = MagicMock()
        conn.execute = AsyncMock(return_value="UPDATE 3")
        monkeypatch.setattr(
            sessions, "get_health_pool", AsyncMock(return_value=_session_pool(conn))
        )

        assert await sessions.finalize_stale_sessions() == 3
        cleanup_sql = conn.execute.await_args.args[0]
        assert "duration_seconds = 0" in cleanup_sql
        assert "request_count *" not in cleanup_sql
