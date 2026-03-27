"""
Smoke-тесты: команды бота.

Сценарии 7.1-7.6 из testing-scenarios.md.
/help — прямой ответ (без SM). /learn, /mode — делегация в SM Dispatcher.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from tests.smoke.conftest import make_intern
from tests.smoke.factories import (
    help_command, learn_command, mode_command,
    progress_command, settings_command, start_command,
    make_command_update,
)


def _fake_reply_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Test")]],
        resize_keyboard=True,
    )


# ─── Общие патчи ───

@pytest.fixture(autouse=True)
def patch_db():
    """Мокаем DB-вызовы для всех команд."""
    with patch("handlers.settings.get_intern", new_callable=AsyncMock) as mock_get_s, \
         patch("handlers.commands.get_intern", new_callable=AsyncMock) as mock_get_c, \
         patch("handlers.commands.is_onboarded", new_callable=AsyncMock) as mock_onb, \
         patch("handlers.progress.get_intern", new_callable=AsyncMock) as mock_get_p, \
         patch("handlers.fallback.get_intern", new_callable=AsyncMock) as mock_get_f:

        intern = make_intern(onboarding_completed=True, name="Тестовый")
        mock_get_s.return_value = intern
        mock_get_c.return_value = intern
        mock_get_p.return_value = intern
        mock_get_f.return_value = intern
        mock_onb.return_value = True

        yield {
            "get_intern_settings": mock_get_s,
            "get_intern_commands": mock_get_c,
            "is_onboarded": mock_onb,
            "get_intern_progress": mock_get_p,
            "get_intern_fallback": mock_get_f,
        }


# ─── 7.2 /help — справка ───

@pytest.mark.asyncio
async def test_help_sends_response(bot, dp, patch_db):
    """Сценарий 7.2: /help возвращает список команд."""
    update = help_command(chat_id=12345)

    await dp.feed_update(bot, update)

    msgs = bot.get_sent("send_message")
    assert len(msgs) > 0, "/help не отправил ответ"


@pytest.mark.asyncio
async def test_help_has_inline_keyboard(bot, dp, patch_db):
    """Сценарий 7.2: /help содержит inline-кнопку для полного списка."""
    update = help_command(chat_id=12345)

    await dp.feed_update(bot, update)

    msgs = bot.get_sent("send_message")
    assert len(msgs) > 0
    has_kb = any(m.get("reply_markup") is not None for m in msgs)
    assert has_kb, "/help не содержит inline-кнопку"


# ─── 7.3 /learn — проверяем onboarding guard ───

@pytest.mark.asyncio
async def test_learn_requires_onboarding(bot, dp, patch_db):
    """Сценарий 7.3: /learn для не-онбордженного пользователя."""
    patch_db["is_onboarded"].return_value = False

    update = learn_command(chat_id=12345)
    await dp.feed_update(bot, update)

    msgs = bot.get_sent("send_message")
    assert len(msgs) > 0, "/learn не ответил не-онбордженному пользователю"


@pytest.mark.asyncio
async def test_learn_delegates_to_dispatcher(bot, dp, patch_db):
    """Сценарий 7.3: /learn делегирует в Dispatcher (если SM активна)."""
    mock_dispatcher = MagicMock()
    mock_dispatcher.is_sm_active = True
    mock_dispatcher.route_learn = AsyncMock()

    with patch("handlers.get_dispatcher", return_value=mock_dispatcher):
        update = learn_command(chat_id=12345)
        await dp.feed_update(bot, update)

    mock_dispatcher.route_learn.assert_called_once()


@pytest.mark.asyncio
async def test_learn_without_sm_shows_error(bot, dp, patch_db):
    """Сценарий 7.3: /learn без SM отвечает ошибкой."""
    with patch("handlers.get_dispatcher", return_value=None):
        update = learn_command(chat_id=12345)
        await dp.feed_update(bot, update)

    msgs = bot.get_sent("send_message")
    assert len(msgs) > 0, "/learn без SM не ответил"


# ─── 7.5 /mode — главное меню ───

@pytest.mark.asyncio
async def test_mode_requires_onboarding(bot, dp, patch_db):
    """Сценарий 7.5: /mode для не-онбордженного пользователя."""
    patch_db["is_onboarded"].return_value = False

    update = mode_command(chat_id=12345)
    await dp.feed_update(bot, update)

    msgs = bot.get_sent("send_message")
    assert len(msgs) > 0, "/mode не ответил не-онбордженному пользователю"


@pytest.mark.asyncio
async def test_mode_delegates_to_dispatcher(bot, dp, patch_db):
    """Сценарий 7.5: /mode делегирует в Dispatcher."""
    mock_dispatcher = MagicMock()
    mock_dispatcher.is_sm_active = True
    mock_dispatcher.route_command = AsyncMock(return_value=True)

    with patch("handlers.get_dispatcher", return_value=mock_dispatcher):
        update = mode_command(chat_id=12345)
        await dp.feed_update(bot, update)

    mock_dispatcher.route_command.assert_called_once()
    call_args = mock_dispatcher.route_command.call_args
    assert call_args[0][0] == "mode", f"route_command вызван с {call_args[0][0]}, ожидалось 'mode'"


# ─── 7.6 /progress — статистика ───

@pytest.mark.asyncio
async def test_progress_delegates_to_dispatcher(bot, dp, patch_db):
    """Сценарий 7.6: /progress делегирует в Dispatcher."""
    mock_dispatcher = MagicMock()
    mock_dispatcher.is_sm_active = True
    mock_dispatcher.route_command = AsyncMock(return_value=True)

    with patch("handlers.get_dispatcher", return_value=mock_dispatcher):
        with patch("handlers.progress.is_onboarded", new_callable=AsyncMock, return_value=True):
            update = progress_command(chat_id=12345)
            await dp.feed_update(bot, update)

    mock_dispatcher.route_command.assert_called_once()


# ─── 7.1 /start (повторный) ───

@pytest.mark.asyncio
async def test_start_existing_does_not_crash(bot, dp, patch_db):
    """Сценарий 7.1: /start для существующего пользователя не падает."""
    with patch("handlers.onboarding.get_intern", new_callable=AsyncMock) as mock_get, \
         patch("handlers.onboarding.update_intern", new_callable=AsyncMock), \
         patch("db.queries.aisystant.get_aisystant_id", new_callable=AsyncMock, return_value=None), \
         patch("db.queries.activity.get_activity_stats", new_callable=AsyncMock, return_value={"total": 5}), \
         patch("core.tier_detector.detect_ui_tier", new_callable=AsyncMock, return_value="T1"), \
         patch("core.tier_ui.build_reply_keyboard", return_value=_fake_reply_kb()), \
         patch("core.tier_ui.sync_menu_commands", new_callable=AsyncMock), \
         patch("core.topics.get_marathon_day", return_value=3):

        mock_get.return_value = make_intern(
            onboarding_completed=True,
            name="Алексей",
            mode="marathon",
        )

        update = start_command(chat_id=12345)
        await dp.feed_update(bot, update)

    # Не упало — уже хорошо
    assert len(bot.sent) > 0, "/start не ответил существующему пользователю"


# ─── Edge: /feed ───

@pytest.mark.asyncio
async def test_feed_delegates_to_dispatcher(bot, dp, patch_db):
    """Сценарий: /feed делегирует в Dispatcher."""
    mock_dispatcher = MagicMock()
    mock_dispatcher.is_sm_active = True
    mock_dispatcher.route_command = AsyncMock(return_value=True)

    with patch("handlers.get_dispatcher", return_value=mock_dispatcher):
        update = make_command_update("/feed", chat_id=12345, user_id=12345)
        await dp.feed_update(bot, update)

    mock_dispatcher.route_command.assert_called_once()
    call_args = mock_dispatcher.route_command.call_args
    assert call_args[0][0] == "feed"
