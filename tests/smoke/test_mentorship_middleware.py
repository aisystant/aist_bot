"""
Smoke-тесты для ArchiveTapMiddleware/ArchiveTapEditMiddleware (WP-578 Ф2).

Тот же контракт, что tests/smoke/test_middleware.py (правило 10.37):
1. Импортируются без ошибок (ловит lazy import в __call__)
2. Инициализируются без ошибок
3. Пропускают событие без краша — и без обращения к БД (enqueue-only)
"""

import sys
import os
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import asyncio
from datetime import datetime

import pytest
from unittest.mock import MagicMock


def _make_fake_message(*, chat_type: str = "group", text: str = "привет всем", user_id: int = 999):
    from aiogram.types import Message, User, Chat

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = -1001
    msg.chat.type = chat_type
    msg.text = text
    msg.message_id = 42
    msg.date = datetime.now()
    msg.reply_to_message = None
    msg.forward_origin = None
    msg.message_thread_id = None
    msg.entities = None
    msg.forum_topic_created = None
    return msg


async def _noop_handler(event, data):
    return "handled"


class TestArchiveTapImports:
    def test_import_archive_tap_middleware(self):
        from engines.mentorship.archive_tap import ArchiveTapMiddleware
        assert ArchiveTapMiddleware is not None

    def test_import_archive_tap_edit_middleware(self):
        from engines.mentorship.archive_tap import ArchiveTapEditMiddleware
        assert ArchiveTapEditMiddleware is not None

    def test_import_worker(self):
        from engines.mentorship.archive_tap import mentorship_archive_worker
        assert mentorship_archive_worker is not None


class TestArchiveTapInit:
    def test_default_queue_is_shared_singleton(self):
        from engines.mentorship.archive_tap import ArchiveTapMiddleware, get_archive_queue

        mw = ArchiveTapMiddleware()
        assert mw._queue is get_archive_queue()

    def test_custom_queue_is_used(self):
        from engines.mentorship.archive_tap import ArchiveTapMiddleware

        own_queue = asyncio.Queue()
        mw = ArchiveTapMiddleware(queue=own_queue)
        assert mw._queue is own_queue


@pytest.mark.asyncio
class TestArchiveTapCall:
    async def test_call_does_not_crash_and_calls_handler(self):
        from engines.mentorship.archive_tap import ArchiveTapMiddleware

        queue: asyncio.Queue = asyncio.Queue()
        mw = ArchiveTapMiddleware(queue=queue)
        msg = _make_fake_message()

        result = await mw(_noop_handler, msg, {})

        assert result == "handled"
        assert queue.qsize() == 1

    async def test_command_is_not_enqueued(self):
        from engines.mentorship.archive_tap import ArchiveTapMiddleware

        queue: asyncio.Queue = asyncio.Queue()
        mw = ArchiveTapMiddleware(queue=queue)
        msg = _make_fake_message(text="/mentor_stream S1")

        await mw(_noop_handler, msg, {})

        assert queue.qsize() == 0, "Служебные команды не должны попадать в очередь архива"

    async def test_channel_post_is_ignored(self):
        """chat.type вне (group, supergroup, private) — не наш периметр."""
        from engines.mentorship.archive_tap import ArchiveTapMiddleware

        queue: asyncio.Queue = asyncio.Queue()
        mw = ArchiveTapMiddleware(queue=queue)
        msg = _make_fake_message(chat_type="channel")

        result = await mw(_noop_handler, msg, {})

        assert result == "handled"
        assert queue.qsize() == 0

    async def test_dm_message_is_enqueued(self):
        from engines.mentorship.archive_tap import ArchiveTapMiddleware, RawMessageEvent

        queue: asyncio.Queue = asyncio.Queue()
        mw = ArchiveTapMiddleware(queue=queue)
        msg = _make_fake_message(chat_type="private", text="у меня вопрос про занятие")

        await mw(_noop_handler, msg, {})

        assert queue.qsize() == 1
        item = queue.get_nowait()
        assert isinstance(item, RawMessageEvent)
        assert item.chat_type == "private"
        assert item.is_edit is False

    async def test_edit_middleware_marks_is_edit(self):
        from engines.mentorship.archive_tap import ArchiveTapEditMiddleware, RawMessageEvent

        queue: asyncio.Queue = asyncio.Queue()
        mw = ArchiveTapEditMiddleware(queue=queue)
        msg = _make_fake_message(text="исправленный текст")

        await mw(_noop_handler, msg, {})

        item = queue.get_nowait()
        assert isinstance(item, RawMessageEvent)
        assert item.is_edit is True

    async def test_queue_full_does_not_raise(self):
        """Переполнение очереди — дроп с логом, не исключение (Р1)."""
        from engines.mentorship.archive_tap import ArchiveTapMiddleware

        tiny_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        tiny_queue.put_nowait(_make_fake_message())  # занять единственное место
        mw = ArchiveTapMiddleware(queue=tiny_queue)
        msg = _make_fake_message()

        result = await mw(_noop_handler, msg, {})

        assert result == "handled", "Переполненная очередь не должна ломать основной путь бота"

    async def test_inner_middleware_never_fires_without_handler(self):
        """Регрессия найденного пир-сессией 24.09 бага: dp.edited_message без
        зарегистрированных handler'ов (как в проде — только middleware) — inner
        middleware, зарегистрированный через .middleware(), не выполняется
        вовсе (aiogram TelegramEventObserver.trigger() оборачивает inner
        только вокруг СОВПАВШЕГО handler'а). Прямой вызов mw(...) в тестах
        выше это не ловит — нужен реальный Router.propagate_event()."""
        from aiogram import Router
        from engines.mentorship.archive_tap import ArchiveTapEditMiddleware

        queue: asyncio.Queue = asyncio.Queue()
        router = Router(name="test-inner-regression")
        router.edited_message.middleware(ArchiveTapEditMiddleware(queue=queue))
        msg = _make_fake_message(text="исправленный текст")

        await router.propagate_event("edited_message", msg)

        assert queue.qsize() == 0, (
            "inner middleware не должен срабатывать без зарегистрированных "
            "handler'ов — если этот assert упал, поведение aiogram изменилось "
            "и .middleware() снова безопасен для edited_message"
        )

    async def test_outer_middleware_fires_without_handler(self):
        """Фикс bot.py:534 (24.09) — outer_middleware выполняется безусловно,
        независимо от наличия handler'ов на dp.edited_message."""
        from aiogram import Router
        from engines.mentorship.archive_tap import ArchiveTapEditMiddleware, RawMessageEvent

        queue: asyncio.Queue = asyncio.Queue()
        router = Router(name="test-outer-fix")
        router.edited_message.outer_middleware(ArchiveTapEditMiddleware(queue=queue))
        msg = _make_fake_message(text="исправленный текст")

        await router.propagate_event("edited_message", msg)

        assert queue.qsize() == 1
        item = queue.get_nowait()
        assert isinstance(item, RawMessageEvent)
        assert item.is_edit is True

    async def test_forum_topic_created_enqueues_topic_event(self):
        from engines.mentorship.archive_tap import ArchiveTapMiddleware, TopicCreatedEvent

        queue: asyncio.Queue = asyncio.Queue()
        mw = ArchiveTapMiddleware(queue=queue)
        msg = _make_fake_message(text=None)
        msg.forum_topic_created = MagicMock()
        msg.message_id = 777

        await mw(_noop_handler, msg, {})

        assert queue.qsize() == 1
        item = queue.get_nowait()
        assert isinstance(item, TopicCreatedEvent)
        assert item.message_thread_id == 777
