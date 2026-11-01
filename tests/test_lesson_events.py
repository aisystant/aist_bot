"""Unit-тесты писателя `lesson_closed.v1` (WP-522 Ф18, факт С3 чек-листа).

Покрывает: `is_first` считается ОДНИМ запросом на весь батч дат из одного
webhook-вызова (не по каждой дате отдельно — иначе bulk-import из нескольких
lesson-файлов в одном коммите получил бы `is_first=true` у всех), а не
у самой ранней даты батча; при уже существующем `lesson_closed` для аккаунта
все даты батча получают `is_first=false`; `external_id` идемпотентен по дате
занятия, не по коммиту.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from core.lesson.events import build_external_id, emit_lesson_closed_batch  # noqa: E402


class _Connection:
    def __init__(self, already_has_lesson: bool):
        self._already_has_lesson = already_has_lesson

    async def fetchval(self, *_args):
        return self._already_has_lesson


class _Acquire:
    def __init__(self, conn: _Connection):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc_info):
        return False


class _Pool:
    def __init__(self, already_has_lesson: bool):
        self._conn = _Connection(already_has_lesson)

    def acquire(self):
        return _Acquire(self._conn)


def _patch_pool(already_has_lesson: bool):
    return patch(
        "core.lesson.events.get_learning_pool",
        new=AsyncMock(return_value=_Pool(already_has_lesson)),
    )


@pytest.mark.asyncio
async def test_first_lesson_ever_marks_earliest_date_in_batch():
    with _patch_pool(already_has_lesson=False), \
         patch("core.lesson.events.post_event", new=AsyncMock()) as post, \
         patch("core.lesson.events.asyncio.create_task", side_effect=lambda coro: coro.close()):
        await emit_lesson_closed_batch("ory-uuid", ["2026-09-12", "2026-09-10", "2026-09-11"])

    assert len(post.call_args_list) == 3
    true_calls = [c for c in post.call_args_list if c.kwargs["payload"]["is_first"] is True]
    assert len(true_calls) == 1
    assert true_calls[0].kwargs["external_id"] == build_external_id("ory-uuid", "2026-09-10")
    for c in post.call_args_list:
        assert c.kwargs["event_type"] == "lesson_closed"
        assert c.kwargs["schema_version"] == "v1"
        assert c.kwargs["source"] == "aist-bot"
        assert c.kwargs["account_id"] == "ory-uuid"


@pytest.mark.asyncio
async def test_account_with_existing_lesson_never_gets_is_first_true():
    with _patch_pool(already_has_lesson=True), \
         patch("core.lesson.events.post_event", new=AsyncMock()) as post, \
         patch("core.lesson.events.asyncio.create_task", side_effect=lambda coro: coro.close()):
        await emit_lesson_closed_batch("ory-uuid", ["2026-09-10", "2026-09-11"])

    assert len(post.call_args_list) == 2
    assert all(c.kwargs["payload"]["is_first"] is False for c in post.call_args_list)


@pytest.mark.asyncio
async def test_empty_batch_posts_nothing():
    with _patch_pool(already_has_lesson=False), \
         patch("core.lesson.events.post_event", new=AsyncMock()) as post, \
         patch("core.lesson.events.asyncio.create_task", side_effect=lambda coro: coro.close()):
        await emit_lesson_closed_batch("ory-uuid", [])

    post.assert_not_called()


def test_external_id_keyed_by_lesson_date_not_commit():
    assert build_external_id("acc", "2026-09-10") == "lesson-closed-acc-2026-09-10"
    assert build_external_id("acc", "2026-09-10") != build_external_id("acc", "2026-09-11")


@pytest.mark.asyncio
async def test_pool_failure_propagates_to_caller():
    """emit_lesson_closed_batch не глотает ошибку сама — вызывающий код
    (github_workbook_webhook_handler) обязан обернуть вызов в try/except,
    иначе сбой learning-пула роняет остаток обработчика вебхука (Critical,
    холодное ревью пир-сессии 2026-09-16-13-wp522-f18-lesson-closed-first)."""
    with patch("core.lesson.events.get_learning_pool", new=AsyncMock(side_effect=RuntimeError("learning pool down"))), \
         patch("core.lesson.events.post_event", new=AsyncMock()) as post:
        with pytest.raises(RuntimeError):
            await emit_lesson_closed_batch("ory-uuid", ["2026-09-10"])

    post.assert_not_called()
