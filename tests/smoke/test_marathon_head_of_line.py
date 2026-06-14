"""Regression-тест head-of-line blocking очереди марафона (fix a490158).

Инцидент 2026-06-13: 13 заблокированных пользователей накопили 226 pending-записей
в `learning.marathon_queue`. Старые pending вытесняли активных из top-100 планировщика
— Артемий @Bulyginoff оказался на позиции 115, урок не доходил.

Корень: очередь не чистилась при блокировке, а `start_marathon_flow` продолжал
создавать записи даже для заблокированного пользователя.

Фикс (a490158) состоит из двух половин, обе должны держаться:
  1. `_handle_unavailable_user` (core/scheduler.py) — чистит очередь сразу после
     пометки `bot_blocked`.
  2. `start_marathon_flow` (handlers/marathon.py) — ранний выход для bot-blocked,
     не создаёт новых записей очереди.

Тест написан в peer-сессии 2026-06-14-20 (WP-418, branch-prep): фикс пришёл без теста,
а слияние ветки доставки с pilot могло «логично» откатить семантику незаметно.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.scheduler as sched
import handlers.marathon as marathon

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ───────────────────── половина 1: чистка очереди при блокировке ─────────────────────

@pytest.mark.asyncio
async def test_unavailable_user_clears_marathon_queue():
    """Блокировка пользователя → очередь марафона очищается (накопление не растёт)."""
    chat_id = 777
    with patch("db.queries.users.mark_bot_blocked", new=AsyncMock()) as mark, \
         patch("db.queries.marathon_newcomer.clear_marathon_queue", new=AsyncMock()) as clear:
        await sched._handle_unavailable_user(chat_id, context="test")

    mark.assert_awaited_once_with(chat_id)
    clear.assert_awaited_once_with(chat_id)   # именно очистка предотвращает накопление pending


@pytest.mark.asyncio
async def test_unavailable_user_survives_clear_failure():
    """Сбой очистки очереди не должен ронять обработку блокировки (try/except + лог)."""
    chat_id = 778
    with patch("db.queries.users.mark_bot_blocked", new=AsyncMock()), \
         patch("db.queries.marathon_newcomer.clear_marathon_queue",
               new=AsyncMock(side_effect=RuntimeError("db down"))):
        # не должно поднять исключение наружу
        await sched._handle_unavailable_user(chat_id, context="test")


# ───────────────────── половина 2: blocked не создаёт новых записей ─────────────────────

@pytest.mark.asyncio
async def test_start_marathon_flow_skips_blocked_user():
    """start_marathon_flow для bot-blocked → ранний выход, очередь не наполняется."""
    reply = MagicMock()
    reply.answer = AsyncMock()
    with patch("db.queries.get_intern", new=AsyncMock(return_value={"bot_blocked": True})), \
         patch.object(marathon, "get_or_create_progress", new=AsyncMock()) as progress:
        await marathon.start_marathon_flow(999, reply, schedule_time="04:00")

    progress.assert_not_awaited()   # ни одной новой записи очереди для заблокированного
    reply.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_marathon_flow_proceeds_for_active_user():
    """Контроль: незаблокированный пользователь проходит гард (get_or_create_progress зовётся)."""
    reply = MagicMock()
    reply.answer = AsyncMock()
    with patch("db.queries.get_intern", new=AsyncMock(return_value={"bot_blocked": False})), \
         patch.object(marathon, "get_or_create_progress",
                      new=AsyncMock(return_value={"status": "active", "current_day": 3})):
        await marathon.start_marathon_flow(1000, reply, schedule_time="04:00")

    reply.answer.assert_awaited()   # активному отвечаем (гард пропустил дальше)


# ───────────────────── скан-инвариант: обе половины фикса на месте ─────────────────────

def _function_body(source: str, signature: str) -> str:
    """Тело функции от её сигнатуры до следующего top-level def/async def."""
    start = source.index(signature)
    rest = source[start + len(signature):]
    next_def = rest.find("\nasync def ")
    alt = rest.find("\ndef ")
    ends = [i for i in (next_def, alt) if i != -1]
    end = min(ends) if ends else len(rest)
    return rest[:end]


def test_fix_anchors_present_in_source():
    """Слияние не должно тихо откатить ни одну половину фикса a490158."""
    scheduler_src = (PROJECT_ROOT / "core" / "scheduler.py").read_text()
    marathon_src = (PROJECT_ROOT / "handlers" / "marathon.py").read_text()

    unavailable = _function_body(scheduler_src, "async def _handle_unavailable_user(")
    assert "clear_marathon_queue" in unavailable, \
        "половина 1 откачена: _handle_unavailable_user больше не чистит очередь"

    start_flow = _function_body(marathon_src, "async def start_marathon_flow(")
    assert "bot_blocked" in start_flow, \
        "половина 2 откачена: start_marathon_flow больше не гейтит заблокированных"
