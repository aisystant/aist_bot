"""
Regression-тесты: Переключение режимов (mode_select).

Сценарии 4.1-4.3: ModeSelectState enter/handle.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from tests.smoke.conftest import make_intern, ThinMockBot
from tests.smoke.sm_helpers import create_state, make_message_obj, make_callback_obj


@pytest.fixture(autouse=True)
def patch_mode_deps():
    """Мокаем зависимости mode_select."""
    with patch("states.common.mode_select.get_intern", new_callable=AsyncMock) as mock_get, \
         patch("states.common.mode_select.update_intern", new_callable=AsyncMock) as mock_upd, \
         patch("states.common.mode_select.detect_ui_tier", new_callable=AsyncMock) as mock_tier, \
         patch("states.common.mode_select.build_reply_keyboard", side_effect=lambda *a, **kw: ReplyKeyboardMarkup(
             keyboard=[[KeyboardButton(text="Test")]], resize_keyboard=True,
         )) as mock_kb, \
         patch("states.common.mode_select.sync_menu_commands", new_callable=AsyncMock) as mock_sync:

        mock_get.return_value = make_intern(onboarding_completed=True, name="Тест")
        mock_tier.return_value = 1  # T1

        yield {
            "get_intern": mock_get,
            "update_intern": mock_upd,
            "detect_tier": mock_tier,
            "build_kb": mock_kb,
            "sync_menu": mock_sync,
        }


def _make_mode_state(bot=None):
    from states.common.mode_select import ModeSelectState
    return create_state(ModeSelectState, bot)


# ─── 4.1 /mode — главное меню ───

async def test_mode_select_enter_sends_greeting(patch_mode_deps):
    """Сценарий 4.1: enter() отправляет приветствие с ReplyKeyboard."""
    state, bot = _make_mode_state()
    user = make_intern(onboarding_completed=True, name="Алексей")

    await state.enter(user)

    assert len(bot.sent) > 0, "mode_select не отправил приветствие"


async def test_mode_select_enter_builds_tier_keyboard(patch_mode_deps):
    """Сценарий 4.1: enter() создаёт tier-based клавиатуру."""
    state, bot = _make_mode_state()
    user = make_intern(onboarding_completed=True)

    await state.enter(user)

    mock_kb = patch_mode_deps["build_kb"]
    assert mock_kb.called, "build_reply_keyboard не был вызван"


async def test_mode_select_syncs_menu_commands(patch_mode_deps):
    """Сценарий 4.1: enter() синхронизирует menu commands."""
    state, bot = _make_mode_state()
    user = make_intern(onboarding_completed=True)

    await state.enter(user)

    mock_sync = patch_mode_deps["sync_menu"]
    assert mock_sync.called, "sync_menu_commands не был вызван"


# ─── 4.2-4.3 handle() — текст в главном меню ───

async def test_mode_select_handle_text_stays(patch_mode_deps):
    """Произвольный текст в mode_select → остаёмся (re-enter)."""
    state, bot = _make_mode_state()
    user = make_intern(onboarding_completed=True)

    msg = make_message_obj(text="привет")
    result = await state.handle(user, msg)

    # handle() возвращает None = остаёмся в стейте
    assert result is None, f"Ожидался None, получен {result}"


# ─── Tier detection ───

async def test_mode_select_detects_tier(patch_mode_deps):
    """enter() вызывает detect_ui_tier для определения уровня."""
    state, bot = _make_mode_state()
    user = make_intern(onboarding_completed=True)

    await state.enter(user)

    mock_tier = patch_mode_deps["detect_tier"]
    assert mock_tier.called, "detect_ui_tier не был вызван"
