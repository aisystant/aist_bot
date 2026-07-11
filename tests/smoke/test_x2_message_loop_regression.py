"""
Regression: X2 message loop (инцидент 2026-07-10/11, WP-7).

save_onboarding_context — read-modify-write, не атомарно. Повторный тап по
stale-кнопке «Освоиться» вызывает run_step заново, и без сериализации
конкурентные вызовы читают состояние ДО того как предыдущий вызов успел
сохранить x2_intro_shown/confirmed — каждый шлёт intro+topic заново.

Тест симулирует DB-задержку (asyncio.sleep между чтением и записью), чтобы
гонка была наблюдаема без реальной БД, и проверяет, что 3 конкурентных
вызова run_step для ОДНОГО chat_id отправляют intro+topic ровно один раз,
а не три.
"""

import asyncio
import os
import sys

import pytest
from unittest.mock import AsyncMock, patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from core.onboarder import x2  # noqa: E402


class _FakeOnboardingStore:
    """In-memory эмуляция current_context['onboarding'] с искусственной задержкой,
    воспроизводящей race window реального read-modify-write через Postgres."""

    def __init__(self, delay: float = 0.02):
        self._data: dict = {}
        self._delay = delay

    async def get(self, chat_id: int) -> dict:
        await asyncio.sleep(self._delay)
        return dict(self._data)

    async def save(self, chat_id: int, patch_dict: dict) -> dict:
        # Читаем-мержим-пишем с той же задержкой, что и get — воспроизводит
        # реальный core/onboarder/storage.py:save_onboarding_context.
        current = dict(self._data)
        await asyncio.sleep(self._delay)
        current.update(patch_dict)
        self._data = current
        return current


@pytest.mark.asyncio
async def test_concurrent_run_step_sends_intro_and_topic_once():
    """Регрессия: 3 конкурентных run_step (тройной тап) -> ровно 1 intro + 1 topic."""
    x2._last_shown.clear()

    chat_id = 999001
    store = _FakeOnboardingStore()
    bot = AsyncMock()
    message = AsyncMock()
    message.bot = bot
    message.chat.id = chat_id
    intern = {"chat_id": chat_id}

    async def fake_get_onboarding_context(cid):
        return await store.get(cid)

    async def fake_save_onboarding_context(cid, patch_dict):
        return await store.save(cid, patch_dict)

    with patch("core.onboarder.storage.get_onboarding_context", side_effect=fake_get_onboarding_context), \
         patch("core.onboarder.storage.save_onboarding_context", side_effect=fake_save_onboarding_context), \
         patch("core.onboarder.x2._finish_x2", new_callable=AsyncMock):
        await asyncio.gather(*(x2.run_step(intern, message) for _ in range(3)))

    intro_calls = [c for c in bot.send_message.await_args_list if c.args[1] == x2._INTRO_TEXT]
    topic_calls = [c for c in bot.send_message.await_args_list if c.kwargs.get("reply_markup") is not None]

    assert len(intro_calls) == 1, f"intro должен отправиться 1 раз, отправился {len(intro_calls)}"
    assert len(topic_calls) == 1, f"topic должен отправиться 1 раз, отправился {len(topic_calls)}"


@pytest.mark.asyncio
async def test_run_step_after_reshow_window_shows_topic_again():
    """Реальный повторный вход (не гонка) после истечения guard-окна — топик показывается."""
    x2._last_shown.clear()

    chat_id = 999002
    store = _FakeOnboardingStore(delay=0)
    bot = AsyncMock()
    message = AsyncMock()
    message.bot = bot
    message.chat.id = chat_id
    intern = {"chat_id": chat_id}

    async def fake_get_onboarding_context(cid):
        return await store.get(cid)

    async def fake_save_onboarding_context(cid, patch_dict):
        return await store.save(cid, patch_dict)

    with patch("core.onboarder.storage.get_onboarding_context", side_effect=fake_get_onboarding_context), \
         patch("core.onboarder.storage.save_onboarding_context", side_effect=fake_save_onboarding_context):
        await x2.run_step(intern, message)
        # Симулируем, что guard-окно истекло (реальный повторный вход, не дубль-тап).
        x2._last_shown[chat_id] = (x2._last_shown[chat_id][0], 0.0)
        await x2.run_step(intern, message)

    topic_calls = [c for c in bot.send_message.await_args_list if c.kwargs.get("reply_markup") is not None]
    assert len(topic_calls) == 2, "После истечения guard-окна повторный вход обязан показать топик"
