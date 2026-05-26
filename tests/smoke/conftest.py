"""
Smoke-тесты: conftest с ThinMockBot и shared fixtures.

Подход: Interface-level mocking (WP-179 вариант J).
Мокаем db/queries/*, clients/claude.py, clients/mcp.py — не транспорт.
dp.feed_update() прогоняет Update через полный aiogram pipeline.
"""

import sys
import os
from pathlib import Path

# Корень проекта должен быть ПЕРВЫМ в sys.path,
# чтобы `import i18n` резолвился из корня, а не из tests/i18n/
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

import asyncio
from datetime import datetime, date
from typing import Optional, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# --- Environment: подставляем env ДО импорта config ---
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("KNOWLEDGE_MCP_URL", "https://fake-mcp.test/mcp")
os.environ.setdefault("USE_STATE_MACHINE", "false")


# ─── ThinMockBot ───

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession


class ThinMockBot(Bot):
    """Минимальный mock Bot — перехватывает все API-вызовы.

    Не делает реальных HTTP-запросов к Telegram API.
    Все вызовы (send_message, answer_callback_query, ...) записываются
    в self.sent для последующих assert-ов.
    """

    def __init__(self, **kwargs):
        # Создаём mock session ДО super().__init__
        self._mock_session = MagicMock(spec=BaseSession)
        self._mock_session.api = MagicMock()

        # Mock для __call__ — все API-вызовы проходят через session
        async def mock_api_call(bot, method, timeout=None):
            return _make_api_response(method)

        self._mock_session.__call__ = mock_api_call
        self._mock_session.close = AsyncMock()

        super().__init__(
            token=os.environ["TELEGRAM_BOT_TOKEN"],
            session=self._mock_session,
        )
        self.sent: List[dict] = []

    async def __call__(self, method, request_timeout=None):
        """Перехват ВСЕХ API-вызовов (send_message, answer, edit, etc.).

        aiogram вызывает bot(method) для любого API-запроса.
        message.answer() → SendMessage → bot(SendMessage(...))
        """
        from aiogram.methods import SendMessage, EditMessageText, EditMessageReplyMarkup
        from aiogram.methods import AnswerCallbackQuery, SendChatAction, DeleteMessage

        method_name = type(method).__name__

        if isinstance(method, SendMessage):
            self.sent.append({
                "method": "send_message",
                "chat_id": method.chat_id,
                "text": method.text,
                "reply_markup": method.reply_markup,
                "parse_mode": method.parse_mode,
            })
            return _make_message(method.chat_id, method.text)

        elif isinstance(method, EditMessageText):
            self.sent.append({
                "method": "edit_message_text",
                "chat_id": method.chat_id,
                "message_id": method.message_id,
                "text": method.text,
                "reply_markup": method.reply_markup,
            })
            return _make_message(method.chat_id or 0, method.text)

        elif isinstance(method, EditMessageReplyMarkup):
            self.sent.append({
                "method": "edit_message_reply_markup",
                "chat_id": method.chat_id,
                "message_id": method.message_id,
                "reply_markup": method.reply_markup,
            })
            return True

        elif isinstance(method, AnswerCallbackQuery):
            self.sent.append({
                "method": "answer_callback_query",
                "callback_query_id": method.callback_query_id,
                "text": method.text,
            })
            return True

        elif isinstance(method, SendChatAction):
            self.sent.append({
                "method": "send_chat_action",
                "chat_id": method.chat_id,
                "action": method.action,
            })
            return True

        elif isinstance(method, DeleteMessage):
            self.sent.append({
                "method": "delete_message",
                "chat_id": method.chat_id,
                "message_id": method.message_id,
            })
            return True

        else:
            # Catch-all для прочих методов
            self.sent.append({
                "method": method_name,
                "raw": str(method),
            })
            return True

    async def set_my_commands(self, *args, **kwargs):
        pass

    async def delete_webhook(self, **kwargs):
        pass

    async def get_me(self):
        from aiogram.types import User as TgUser
        return TgUser(id=self.id, is_bot=True, first_name="TestBot", username="test_bot")

    @property
    def id(self) -> int:
        return 000000000

    def get_sent(self, method: str = "send_message") -> list[dict]:
        """Получить все вызовы определённого метода."""
        return [s for s in self.sent if s["method"] == method]

    def last_sent_text(self) -> Optional[str]:
        """Текст последнего send_message."""
        msgs = self.get_sent("send_message")
        return msgs[-1]["text"] if msgs else None

    def clear_sent(self):
        self.sent.clear()


# ─── Helpers ───

def _make_message(chat_id: int, text: str):
    """Создаёт минимальный Message-like объект для возврата из send_message."""
    from aiogram.types import Message, Chat, User as TgUser
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=chat_id, is_bot=False, first_name="Test"),
        text=text,
    )


def _make_api_response(method):
    """Default response для API-вызовов через session."""
    return True


# ─── Default intern dict ───

def make_intern(
    chat_id: int = 12345,
    name: str = "Тестовый",
    onboarding_completed: bool = False,
    language: str = "ru",
    mode: str = "marathon",
    **overrides,
) -> dict:
    """Создаёт dict пользователя, аналогичный get_intern()."""
    base = {
        "user_id": 1,
        "chat_id": chat_id,
        "name": name,
        "occupation": None,
        "role": None,
        "domain": None,
        "interests": None,
        "motivation": None,
        "goals": None,
        "language": language,
        "experience_level": "beginner",
        "difficulty_preference": None,
        "learning_style": None,
        "study_duration": "15",
        "current_problems": None,
        "desires": None,
        "tg_username": "testuser",
        "aisystant_id": None,
        "aisystant_linked_at": None,
        "dt_connected_at": None,
        "dt_user_id": None,
        "tier": "T1",
        "email": None,
        "timezone": None,
        "created_at": datetime(2026, 1, 1),
        "mode": mode,
        "current_context": {},
        "current_state": None,
        "topic_order": None,
        "schedule_time": "09:00",
        "schedule_time_2": None,
        "feed_schedule_time": None,
        "marathon_status": None,
        "marathon_start_date": None,
        "marathon_paused_at": None,
        "current_topic_index": 0,
        "completed_topics": [],
        "topics_today": [],
        "last_topic_date": None,
        "complexity_level": 1,
        "topics_at_current_complexity": 0,
        "feed_status": None,
        "feed_started_at": None,
        "active_days_total": 0,
        "active_days_streak": 0,
        "longest_streak": 0,
        "last_active_date": None,
        "bot_blocked": False,
        "bot_blocked_at": None,
        "bot_recheck_at": None,
        "trial_started_at": None,
        "assessment_state": None,
        "assessment_date": None,
        "stats_reset_date": None,
        "notify_template_updates": True,
        "onboarding_completed": onboarding_completed,
        "updated_at": datetime(2026, 1, 1),
    }
    base.update(overrides)
    return base


# ─── Fixtures ───

@pytest.fixture
def bot():
    """ThinMockBot instance — создаётся заново на каждый тест (чистый sent)."""
    return ThinMockBot()


# Singleton dispatcher — роутеры attach один раз на сессию
_dp_instance = None


def _get_dispatcher():
    """Ленивый singleton Dispatcher с роутерами."""
    global _dp_instance
    if _dp_instance is None:
        from aiogram.fsm.storage.memory import MemoryStorage

        _dp_instance = Dispatcher(storage=MemoryStorage())

        from handlers.onboarding import onboarding_router
        from handlers.commands import commands_router
        from handlers.callbacks import callbacks_router
        from handlers.settings import settings_router
        from handlers.progress import progress_router
        from handlers.fallback import fallback_router

        _dp_instance.include_router(onboarding_router)
        _dp_instance.include_router(commands_router)
        _dp_instance.include_router(callbacks_router)
        _dp_instance.include_router(settings_router)
        _dp_instance.include_router(progress_router)
        _dp_instance.include_router(fallback_router)

    return _dp_instance


@pytest.fixture
def dp():
    """Dispatcher с подключёнными роутерами.

    Singleton — роутеры aiogram не поддерживают повторный attach.
    """
    return _get_dispatcher()


@pytest.fixture
def mock_get_intern():
    """Mock db.queries.get_intern — возвращает make_intern() по умолчанию."""
    with patch("db.queries.users.get_pool", new_callable=AsyncMock):
        with patch("db.queries.get_intern", new_callable=AsyncMock) as mock:
            mock.return_value = make_intern()
            yield mock


@pytest.fixture
def mock_update_intern():
    """Mock db.queries.update_intern."""
    with patch("db.queries.update_intern", new_callable=AsyncMock) as mock:
        yield mock


@pytest.fixture
def mock_claude():
    """Mock ClaudeClient — все generate* методы возвращают текст."""
    with patch("clients.claude.ClaudeClient.generate_content", new_callable=AsyncMock) as gen_content, \
         patch("clients.claude.ClaudeClient.generate_question", new_callable=AsyncMock) as gen_question, \
         patch("clients.claude.ClaudeClient.generate_practice_intro", new_callable=AsyncMock) as gen_practice:
        gen_content.return_value = "Тестовый урок: содержание"
        gen_question.return_value = "Тестовый вопрос?"
        gen_practice.return_value = "Тестовое задание: описание"
        yield {
            "generate_content": gen_content,
            "generate_question": gen_question,
            "generate_practice_intro": gen_practice,
        }


@pytest.fixture
def mock_mcp():
    """Mock MCP knowledge client."""
    with patch("clients.mcp.MCPClient.search", new_callable=AsyncMock) as mock:
        mock.return_value = []
        yield mock


@pytest.fixture
def mock_db_pool():
    """Mock DB connection pool — предотвращает реальные подключения к БД."""
    with patch("db.connection.get_pool", new_callable=AsyncMock) as mock:
        mock_pool = AsyncMock()
        mock.return_value = mock_pool
        yield mock_pool


@pytest.fixture
def all_mocks(mock_get_intern, mock_update_intern, mock_claude, mock_mcp, mock_db_pool):
    """Convenience: все моки вместе."""
    return {
        "get_intern": mock_get_intern,
        "update_intern": mock_update_intern,
        "claude": mock_claude,
        "mcp": mock_mcp,
        "db_pool": mock_db_pool,
    }
