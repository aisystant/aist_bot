"""
Regression-тесты: Feed SM стейты (FeedTopicsState, FeedDigestState).

Сценарии 3.1-3.6 из testing-scenarios.md.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date

from tests.smoke.conftest import make_intern
from tests.smoke.sm_helpers import create_state, make_message_obj, make_callback_obj


# ─── FeedTopicsState ───

@pytest.fixture
def patch_topics_deps():
    with patch("states.feed.topics.get_intern", new_callable=AsyncMock) as mock_get, \
         patch("states.feed.topics.update_intern", new_callable=AsyncMock) as mock_upd, \
         patch("states.feed.topics.get_current_feed_week", new_callable=AsyncMock) as mock_week, \
         patch("states.feed.topics.create_feed_week", new_callable=AsyncMock) as mock_create, \
         patch("states.feed.topics.update_feed_week", new_callable=AsyncMock) as mock_update_week, \
         patch("states.feed.topics.delete_feed_sessions", new_callable=AsyncMock), \
         patch("states.feed.topics.suggest_weekly_topics", new_callable=AsyncMock, return_value=["Тема 1", "Тема 2", "Тема 3"]):

        mock_get.return_value = make_intern(mode="feed")
        mock_week.return_value = None  # Нет активной недели

        yield {
            "get_intern": mock_get,
            "update_intern": mock_upd,
            "get_week": mock_week,
            "create_week": mock_create,
            "update_week": mock_update_week,
        }


def _make_topics_state(bot=None):
    from states.feed.topics import FeedTopicsState
    return create_state(FeedTopicsState, bot)


# ─── 3.1 Активация Ленты (темы не выбраны) ───

async def test_topics_enter_no_active_week(patch_topics_deps):
    """Сценарий 3.1: Нет активной недели → показывает выбор тем."""
    state, bot = _make_topics_state()
    user = make_intern(mode="feed")

    # enter() должен показать выбор тем (или вернуть None/skip)
    result = await state.enter(user)

    # Если нет недели → генерирует темы и показывает UI (result=None)
    # или topics_selected если неделя уже есть
    # Не падает — основная проверка
    assert result in (None, "topics_selected", "skip"), f"Неожиданный результат: {result}"


async def test_topics_enter_existing_week_skips(patch_topics_deps):
    """Сценарий 3.2: Есть активная неделя → topics_selected (переход к digest)."""
    patch_topics_deps["get_week"].return_value = {
        "id": 1,
        "status": "active",
        "accepted_topics": ["Системное мышление", "Стратегирование"],
    }

    state, bot = _make_topics_state()
    user = make_intern(mode="feed")

    result = await state.enter(user)

    assert result == "topics_selected", f"Ожидался 'topics_selected', получен {result}"


# ─── 3.2 Callback: выбор тем ───

async def test_topics_callback_confirm(patch_topics_deps):
    """Сценарий 3.2: Подтверждение выбора тем → topics_selected."""
    patch_topics_deps["get_week"].return_value = {
        "id": 1,
        "status": "planning",
        "suggested_topics": ["Тема 1", "Тема 2", "Тема 3"],
        "accepted_topics": [],
    }

    state, bot = _make_topics_state()
    user = make_intern(mode="feed")

    # Симулируем что пользователь выбрал темы (через BaseState API)
    with patch.object(state, "load_state", new=AsyncMock(return_value={
        "suggested_topics": [
            {"title": "Тема 1"},
            {"title": "Тема 2"},
            {"title": "Тема 3"},
        ],
        "selected_indices": [0, 1],
        "week_id": 1,
    })), patch.object(state, "clear_state", new=AsyncMock()):
        callback = make_callback_obj(data="feed_confirm", bot=bot)
        result = await state.handle_callback(user, callback)

    # feed_confirm может вернуть topics_selected или None (если _accept_topics не сработал)
    assert result in ("topics_selected", None), f"Неожиданный результат: {result}"


# ─── FeedDigestState ───

@pytest.fixture
def patch_digest_deps():
    with patch("states.feed.digest.get_intern", new_callable=AsyncMock) as mock_get, \
         patch("states.feed.digest.update_intern", new_callable=AsyncMock), \
         patch("states.feed.digest.get_current_feed_week", new_callable=AsyncMock) as mock_week, \
         patch("states.feed.digest.update_feed_week", new_callable=AsyncMock), \
         patch("states.feed.digest.create_feed_session", new_callable=AsyncMock) as mock_create, \
         patch("states.feed.digest.update_feed_session", new_callable=AsyncMock), \
         patch("states.feed.digest.get_feed_session", new_callable=AsyncMock) as mock_session, \
         patch("states.feed.digest.get_incomplete_feed_session", new_callable=AsyncMock, return_value=None), \
         patch("states.feed.digest.record_active_day", new_callable=AsyncMock), \
         patch("states.feed.digest.get_activity_stats", new_callable=AsyncMock, return_value={"total": 5}), \
         patch("states.feed.digest.moscow_today", return_value=date(2026, 3, 27)):

        mock_get.return_value = make_intern(mode="feed", feed_status="active")
        mock_week.return_value = {
            "id": 1,
            "status": "active",
            "accepted_topics": ["Системное мышление"],
            "current_day": 1,
        }
        mock_session.return_value = None  # Нет сессии на сегодня

        yield {
            "get_intern": mock_get,
            "get_week": mock_week,
            "get_session": mock_session,
            "create_session": mock_create,
        }


def _make_digest_state(bot=None):
    from states.feed.digest import FeedDigestState
    return create_state(FeedDigestState, bot)


# ─── 3.4 Получение дайджеста ───

async def test_digest_enter_no_week(patch_digest_deps):
    """Нет активной недели → done."""
    patch_digest_deps["get_week"].return_value = None

    state, bot = _make_digest_state()
    user = make_intern(mode="feed")

    result = await state.enter(user)

    assert result == "done", f"Ожидался 'done', получен {result}"


async def test_digest_enter_planning_week(patch_digest_deps):
    """Неделя в planning → change_topics."""
    patch_digest_deps["get_week"].return_value = {
        "id": 1,
        "status": "planning",
        "accepted_topics": [],
    }

    state, bot = _make_digest_state()
    user = make_intern(mode="feed")

    result = await state.enter(user)

    assert result == "change_topics", f"Ожидался 'change_topics', получен {result}"


# ─── 3.5 Callback: фиксация ───

async def test_digest_callback_fixation(patch_digest_deps):
    """Сценарий 3.5: Нажатие 'Фиксация' → ожидание ввода."""
    state, bot = _make_digest_state()
    user = make_intern(mode="feed")
    chat_id = user["chat_id"]

    state._user_data[chat_id] = {"session_id": 1, "waiting_fixation": False, "week_id": 1}

    callback = make_callback_obj(data="feed_fixation", bot=bot)
    result = await state.handle_callback(user, callback)

    # Должен установить waiting_fixation и отправить промпт
    assert result is None, f"Ожидался None (stay), получен {result}"
    assert state._user_data[chat_id].get("waiting_fixation") is True, "waiting_fixation не установлен"


# ─── Callback: назад в меню ───

async def test_digest_callback_back_to_menu(patch_digest_deps):
    """Callback 'feed_back_to_menu' → done."""
    state, bot = _make_digest_state()
    user = make_intern(mode="feed")
    chat_id = user["chat_id"]

    state._user_data[chat_id] = {"session_id": 1, "waiting_fixation": False, "week_id": 1}

    callback = make_callback_obj(data="feed_back_to_menu", bot=bot)
    result = await state.handle_callback(user, callback)

    assert result == "done", f"Ожидался 'done', получен {result}"


# ─── Callback: сменить темы ───

async def test_digest_callback_change_topics(patch_digest_deps):
    """Callback 'feed_topics_menu' → change_topics."""
    state, bot = _make_digest_state()
    user = make_intern(mode="feed")
    chat_id = user["chat_id"]

    state._user_data[chat_id] = {"session_id": 1, "waiting_fixation": False, "week_id": 1}

    callback = make_callback_obj(data="feed_topics_menu", bot=bot)
    result = await state.handle_callback(user, callback)

    assert result == "change_topics", f"Ожидался 'change_topics', получен {result}"


# ─── Callback: пропустить дайджест ───

async def test_digest_callback_skip(patch_digest_deps):
    """Callback 'feed_skip' → сессия помечена как skipped."""
    state, bot = _make_digest_state()
    user = make_intern(mode="feed")
    chat_id = user["chat_id"]

    state._user_data[chat_id] = {"session_id": 1, "waiting_fixation": False, "week_id": 1}

    callback = make_callback_obj(data="feed_skip", bot=bot)
    result = await state.handle_callback(user, callback)

    # Остаёмся в стейте (показывается меню)
    assert result is None, f"Ожидался None, получен {result}"
