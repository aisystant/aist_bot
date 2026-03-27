"""
Regression-тесты: Граничные случаи.

Сценарии 8.1-8.9 из testing-scenarios.md.
Используют dp fixture (singleton Dispatcher) из conftest.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from tests.smoke.conftest import make_intern
from tests.smoke.factories import text_message, callback_button, help_command


@pytest.fixture(autouse=True)
def patch_edge_deps():
    """Мокаем DB для edge case тестов."""
    with patch("handlers.fallback.get_intern", new_callable=AsyncMock) as mock_get, \
         patch("handlers.settings.get_intern", new_callable=AsyncMock) as mock_get_s:
        mock_get.return_value = make_intern(onboarding_completed=True)
        mock_get_s.return_value = make_intern(onboarding_completed=True)
        yield {"get_intern": mock_get}


# ─── 8.2 Очень длинное сообщение ───

async def test_long_message_does_not_crash(bot, dp, patch_edge_deps):
    """Сценарий 8.2: 4000+ символов — бот не падает."""
    long_text = "А" * 5000
    update = text_message(long_text, chat_id=12345)
    await dp.feed_update(bot, update)
    assert True


# ─── 8.3 Сообщение с эмодзи ───

async def test_emoji_message(bot, dp, patch_edge_deps):
    """Сценарий 8.3: Эмодзи обрабатываются корректно."""
    update = text_message("Привет! 😊🎉🚀", chat_id=12345)
    await dp.feed_update(bot, update)
    assert True


# ─── 8.4 Нелатиница ───

async def test_non_latin_message(bot, dp, patch_edge_deps):
    """Сценарий 8.4: Арабский, китайский текст не ломает бота."""
    for text in ["مرحبا", "你好世界", "こんにちは"]:
        bot.clear_sent()
        update = text_message(text, chat_id=12345)
        await dp.feed_update(bot, update)
    assert True


# ─── 8.6 Stale callback (>30 min) ───

async def test_stale_callback_handled(bot, dp, patch_edge_deps):
    """Сценарий 8.6: Старая кнопка → fallback обрабатывает."""
    update = callback_button(data="very_old_callback_12345", chat_id=12345)
    await dp.feed_update(bot, update)
    # Fallback должен ответить (answer_callback_query или send_message)
    assert len(bot.sent) > 0, "Stale callback не обработан"


# ─── 8.1 Пустое текстовое сообщение (пробелы) ───

async def test_whitespace_message(bot, dp, patch_edge_deps):
    """Сценарий 8.1: Пустое/пробельное сообщение не ломает бота."""
    update = text_message("   ", chat_id=12345)
    await dp.feed_update(bot, update)
    assert True


# ─── 8.9 Несколько команд подряд ───

async def test_rapid_commands_no_crash(bot, dp, patch_edge_deps):
    """Сценарий 8.9: Быстрые команды подряд."""
    for _ in range(5):
        bot.clear_sent()
        update = help_command(chat_id=12345)
        await dp.feed_update(bot, update)
    assert True
