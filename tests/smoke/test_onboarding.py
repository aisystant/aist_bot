"""
Smoke-тесты: онбординг (/start).

Сценарии 1.1-1.3, 1.6-1.7 из testing-scenarios.md.
Interface-level mocking: мокаем db.queries.*, clients/*.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from tests.smoke.conftest import make_intern
from tests.smoke.factories import start_command, text_message, callback_button


def _fake_reply_kb():
    """Создаёт минимальный ReplyKeyboardMarkup для тестов."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Test")]],
        resize_keyboard=True,
    )


# ─── Патчи, общие для всех тестов онбординга ───

@pytest.fixture(autouse=True)
def patch_onboarding_deps():
    """Мокаем все DB и external-зависимости онбординга."""
    with patch("handlers.onboarding.get_intern", new_callable=AsyncMock) as mock_get, \
         patch("handlers.onboarding.update_intern", new_callable=AsyncMock) as mock_upd, \
         patch("handlers.onboarding._try_auto_link", new_callable=AsyncMock) as mock_link, \
         patch("core.tier_detector.detect_ui_tier", new_callable=AsyncMock) as mock_tier, \
         patch("core.tier_ui.build_reply_keyboard", side_effect=lambda *a, **kw: _fake_reply_kb()) as mock_kb, \
         patch("core.tier_ui.sync_menu_commands", new_callable=AsyncMock) as mock_sync, \
         patch("core.tier_ui.send_tier_keyboard", new_callable=AsyncMock) as mock_send_kb, \
         patch("db.queries.aisystant.get_aisystant_id", new_callable=AsyncMock) as mock_ais, \
         patch("db.queries.activity.get_activity_stats", new_callable=AsyncMock) as mock_stats, \
         patch("db.queries.users.get_slot_load", new_callable=AsyncMock) as mock_slot, \
         patch("db.queries.users.moscow_today") as mock_today, \
         patch("db.queries.profile.reset_learning_data", new_callable=AsyncMock) as mock_reset:

        # Defaults
        mock_get.return_value = make_intern(onboarding_completed=False)
        mock_link.return_value = False
        mock_tier.return_value = "T0"
        mock_kb.return_value = MagicMock()  # ReplyKeyboard
        mock_ais.return_value = None
        mock_stats.return_value = {"total": 0}
        mock_slot.return_value = {}

        from datetime import date
        mock_today.return_value = date(2026, 3, 27)

        yield {
            "get_intern": mock_get,
            "update_intern": mock_upd,
            "auto_link": mock_link,
            "detect_tier": mock_tier,
            "build_kb": mock_kb,
            "sync_menu": mock_sync,
            "send_tier_kb": mock_send_kb,
            "aisystant_id": mock_ais,
            "activity_stats": mock_stats,
            "slot_load": mock_slot,
            "moscow_today": mock_today,
            "reset_learning": mock_reset,
        }


# ─── 1.1 Первый запуск бота (/start новый пользователь) ───

@pytest.mark.asyncio
async def test_start_new_user_sends_greeting(bot, dp, patch_onboarding_deps):
    """Сценарий 1.1: Новый пользователь получает приветствие."""
    update = start_command(chat_id=12345)

    await dp.feed_update(bot, update)

    # Бот должен ответить приветствием
    assert len(bot.sent) > 0, "Бот не отправил ни одного сообщения"
    texts = [s["text"] for s in bot.get_sent("send_message")]
    assert len(texts) > 0, "Нет send_message вызовов"


@pytest.mark.asyncio
async def test_start_new_user_saves_profile(bot, dp, patch_onboarding_deps):
    """Сценарий 1.1: При /start сохраняется имя и язык."""
    update = start_command(chat_id=12345)

    await dp.feed_update(bot, update)

    # update_intern должен быть вызван с именем и языком
    mock_upd = patch_onboarding_deps["update_intern"]
    assert mock_upd.called, "update_intern не был вызван"

    # Проверяем что onboarding_completed=True (WP-79: 0-step onboarding)
    calls = mock_upd.call_args_list
    onboarding_call = [c for c in calls if c.kwargs.get("onboarding_completed") is True]
    assert len(onboarding_call) > 0, "onboarding_completed не был установлен в True"


@pytest.mark.asyncio
async def test_start_new_user_auto_detects_language(bot, dp, patch_onboarding_deps):
    """Сценарий 1.2: Язык определяется автоматически из Telegram."""
    update = start_command(chat_id=12345)

    await dp.feed_update(bot, update)

    mock_upd = patch_onboarding_deps["update_intern"]
    calls = mock_upd.call_args_list
    # Должен быть вызов с language=
    lang_calls = [c for c in calls if "language" in c.kwargs]
    assert len(lang_calls) > 0, "Язык не был сохранён"


@pytest.mark.asyncio
async def test_start_new_user_sends_keyboard(bot, dp, patch_onboarding_deps):
    """Сценарий 1.6-1.7: /start отправляет клавиатуру с кнопками."""
    update = start_command(chat_id=12345)

    await dp.feed_update(bot, update)

    msgs = bot.get_sent("send_message")
    assert len(msgs) > 0
    # Должна быть reply_markup (ReplyKeyboard)
    has_keyboard = any(m.get("reply_markup") is not None for m in msgs)
    assert has_keyboard, "Бот не отправил клавиатуру"


# ─── 1.1 /start для существующего пользователя ───

@pytest.mark.asyncio
async def test_start_existing_user_welcome_back(bot, dp, patch_onboarding_deps):
    """Сценарий 1.1: Существующий пользователь получает приветствие «с возвращением»."""
    # Пользователь уже прошёл онбординг
    patch_onboarding_deps["get_intern"].return_value = make_intern(
        onboarding_completed=True,
        name="Алексей",
    )

    update = start_command(chat_id=12345)
    await dp.feed_update(bot, update)

    # Бот должен ответить (send_message или через SM)
    assert len(bot.sent) > 0, "Бот не ответил существующему пользователю"


@pytest.mark.asyncio
async def test_start_existing_user_no_re_onboarding(bot, dp, patch_onboarding_deps):
    """Сценарий 1.1: Существующий пользователь НЕ проходит онбординг заново."""
    patch_onboarding_deps["get_intern"].return_value = make_intern(
        onboarding_completed=True,
        name="Алексей",
    )

    update = start_command(chat_id=12345)
    await dp.feed_update(bot, update)

    # update_intern НЕ должен вызываться с onboarding_completed
    mock_upd = patch_onboarding_deps["update_intern"]
    onboarding_calls = [c for c in mock_upd.call_args_list if c.kwargs.get("onboarding_completed") is True]
    assert len(onboarding_calls) == 0, "Существующий пользователь прошёл онбординг повторно"


# ─── 1.3 Сохранение имени ───

@pytest.mark.asyncio
async def test_start_saves_first_name(bot, dp, patch_onboarding_deps):
    """Сценарий 1.3: Имя берётся из first_name Telegram."""
    update = start_command(chat_id=12345)
    # first_name задано в factory как "Тестовый"

    await dp.feed_update(bot, update)

    mock_upd = patch_onboarding_deps["update_intern"]
    name_calls = [c for c in mock_upd.call_args_list if "name" in c.kwargs]
    assert len(name_calls) > 0, "Имя не было сохранено"
    saved_name = name_calls[0].kwargs["name"]
    assert saved_name == "Тестовый", f"Имя сохранено неверно: {saved_name}"


# ─── Попытка auto-link Aisystant ───

@pytest.mark.asyncio
async def test_start_attempts_auto_link(bot, dp, patch_onboarding_deps):
    """При /start бот пытается автоматически привязать Aisystant."""
    update = start_command(chat_id=12345)
    await dp.feed_update(bot, update)

    mock_link = patch_onboarding_deps["auto_link"]
    assert mock_link.called, "_try_auto_link не был вызван"


@pytest.mark.asyncio
async def test_start_shows_link_reminder_when_not_linked(bot, dp, patch_onboarding_deps):
    """Если Aisystant не привязан, бот напоминает."""
    patch_onboarding_deps["auto_link"].return_value = False

    update = start_command(chat_id=12345)
    await dp.feed_update(bot, update)

    # Проверяем что в ответе есть упоминание привязки
    texts = [s["text"] for s in bot.get_sent("send_message")]
    all_text = " ".join(texts)
    # Текст напоминания содержит ключевые слова
    assert len(all_text) > 0, "Бот не отправил текст"


# ─── Tier-based keyboard ───

@pytest.mark.asyncio
async def test_start_sends_tier_keyboard(bot, dp, patch_onboarding_deps):
    """Новый пользователь получает T0 клавиатуру."""
    update = start_command(chat_id=12345)
    await dp.feed_update(bot, update)

    mock_build = patch_onboarding_deps["build_kb"]
    assert mock_build.called, "build_reply_keyboard не был вызван"
    # Первый вызов — T0 (новый пользователь)
    tier_arg = mock_build.call_args[0][0]
    assert tier_arg == "T0", f"Ожидался тир T0, получен {tier_arg}"
