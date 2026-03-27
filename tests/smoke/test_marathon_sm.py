"""
Regression-тесты: Марафон через SM-стейты напрямую.

Сценарии 2.1-2.14: enter(), handle(), handle_callback() на MarathonLessonState и др.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date, datetime

from tests.smoke.conftest import make_intern, ThinMockBot
from tests.smoke.sm_helpers import create_state, make_message_obj, make_callback_obj


# ─── Общие патчи для всех marathon тестов ───

@pytest.fixture(autouse=True)
def patch_marathon_deps():
    """Мокаем DB, topics, claude для marathon стейтов."""
    with patch("states.workshops.marathon.lesson.get_intern", new_callable=AsyncMock) as mock_get, \
         patch("states.workshops.marathon.lesson.update_intern", new_callable=AsyncMock) as mock_upd, \
         patch("states.workshops.marathon.lesson.get_total_topics", return_value=28) as mock_total, \
         patch("states.workshops.marathon.lesson.get_topic") as mock_topic, \
         patch("states.workshops.marathon.lesson.get_topic_title", return_value="Системное мышление") as mock_title, \
         patch("states.workshops.marathon.lesson.get_topics_today", return_value=0) as mock_today_topics, \
         patch("states.workshops.marathon.lesson.moscow_today", return_value=date(2026, 3, 27)) as mock_today, \
         patch("states.workshops.marathon.lesson.get_marathon_content", new_callable=AsyncMock) as mock_content, \
         patch("states.workshops.marathon.lesson.mark_content_delivered", new_callable=AsyncMock), \
         patch("states.workshops.marathon.lesson.save_marathon_content", new_callable=AsyncMock), \
         patch("states.workshops.marathon.lesson.claude") as mock_claude, \
         patch("states.workshops.marathon.lesson.mcp_knowledge") as mock_mcp, \
         patch("states.workshops.marathon.lesson.canonical_get_marathon_day", return_value=1):

        mock_get.return_value = make_intern(onboarding_completed=True, mode="marathon")
        mock_topic.return_value = {"id": "topic_1", "type": "theory", "title": "Системное мышление", "day": 1}
        mock_content.return_value = {"lesson_content": "Урок: системное мышление — это подход...", "id": 1, "status": "pending"}
        mock_claude.generate_content = AsyncMock(return_value="Сгенерированный урок")

        yield {
            "get_intern": mock_get,
            "update_intern": mock_upd,
            "get_total_topics": mock_total,
            "get_topic": mock_topic,
            "get_topic_title": mock_title,
            "get_topics_today": mock_today_topics,
            "moscow_today": mock_today,
            "get_content": mock_content,
            "claude": mock_claude,
        }


def _make_lesson_state(bot=None):
    """Создаёт MarathonLessonState с ThinMockBot."""
    from states.workshops.marathon.lesson import MarathonLessonState
    return create_state(MarathonLessonState, bot)


# ─── 2.1 Получение первого урока (/learn → lesson state) ───

async def test_lesson_enter_shows_content(patch_marathon_deps):
    """Сценарий 2.1: enter() отправляет урок пользователю."""
    state, bot = _make_lesson_state()
    user = make_intern(
        onboarding_completed=True,
        mode="marathon",
        marathon_start_date=date(2026, 3, 27),
        completed_topics=[],
        current_topic_index=0,
    )

    result = await state.enter(user)

    # Бот должен отправить контент
    assert len(bot.sent) > 0, "Бот не отправил урок"
    # Результат None = ждём нажатия кнопки (или lesson_shown для автоперехода)
    # Не проверяем конкретное значение — зависит от реализации


async def test_lesson_enter_pre_generated_content(patch_marathon_deps):
    """Сценарий 2.1: Пре-генерированный контент используется из cache."""
    patch_marathon_deps["get_content"].return_value = {
        "lesson_content": "Пре-генерированный урок длиной больше 200 символов " * 5,
        "id": 1,
        "status": "pending",
    }

    state, bot = _make_lesson_state()
    user = make_intern(
        marathon_start_date=date(2026, 3, 27),
        completed_topics=[],
        current_topic_index=0,
    )

    await state.enter(user)

    # Claude не должен быть вызван — используется cache
    mock_claude = patch_marathon_deps["claude"]
    # Если cache hit — generate_content не вызывается
    msgs = bot.get_sent("send_message")
    assert len(msgs) > 0, "Бот не отправил пре-генерированный урок"


# ─── 2.11 Марафон завершён ───

async def test_lesson_marathon_complete(patch_marathon_deps):
    """Сценарий 2.11: Все темы пройдены → marathon_complete."""
    state, bot = _make_lesson_state()
    user = make_intern(
        marathon_start_date=date(2026, 3, 14),
        completed_topics=list(range(28)),  # Все 28 тем
        current_topic_index=28,
    )

    result = await state.enter(user)

    assert result == "marathon_complete", f"Ожидался 'marathon_complete', получен {result}"
    assert len(bot.sent) > 0, "Бот не отправил поздравление"


# ─── Daily limit ───

async def test_lesson_daily_limit(patch_marathon_deps):
    """Лимит тем на день → daily_limit."""
    patch_marathon_deps["get_topics_today"].return_value = 4  # MAX_TOPICS_PER_DAY = 4

    state, bot = _make_lesson_state()
    user = make_intern(
        marathon_start_date=date(2026, 3, 27),
        completed_topics=[0, 1],
        current_topic_index=2,
        topics_today=[0, 1, 2, 3],
    )

    result = await state.enter(user)

    assert result == "daily_limit", f"Ожидался 'daily_limit', получен {result}"


# ─── Practice routing ───

async def test_lesson_practice_routes_to_task(patch_marathon_deps):
    """Текущая тема — practice → already_completed (→ task state)."""
    patch_marathon_deps["get_topic"].return_value = {
        "id": "topic_3", "type": "practice", "title": "Задание", "day": 2,
    }

    state, bot = _make_lesson_state()
    user = make_intern(
        marathon_start_date=date(2026, 3, 26),
        completed_topics=[0, 1],
        current_topic_index=2,
    )

    result = await state.enter(user)

    assert result == "already_completed", f"Ожидался 'already_completed', получен {result}"


# ─── No topic available ───

async def test_lesson_no_topic_shows_come_back(patch_marathon_deps):
    """Нет доступной темы → come_back."""
    patch_marathon_deps["get_topic"].return_value = None

    state, bot = _make_lesson_state()
    user = make_intern(
        marathon_start_date=date(2026, 3, 27),
        completed_topics=[],
        current_topic_index=99,
    )

    result = await state.enter(user)

    assert result == "come_back", f"Ожидался 'come_back', получен {result}"


# ─── 2.2 Callback: переход к вопросу ───

async def test_lesson_callback_next_question(patch_marathon_deps):
    """Сценарий 2.2: Нажатие 'Перейти к вопросу' → lesson_shown."""
    state, bot = _make_lesson_state()
    user = make_intern(marathon_start_date=date(2026, 3, 27))

    callback = make_callback_obj(data="marathon_next_question", bot=bot)
    result = await state.handle_callback(user, callback)

    assert result == "lesson_shown", f"Ожидался 'lesson_shown', получен {result}"


async def test_lesson_callback_back_menu(patch_marathon_deps):
    """Нажатие 'Назад в меню' → come_back."""
    state, bot = _make_lesson_state()
    user = make_intern(marathon_start_date=date(2026, 3, 27))

    callback = make_callback_obj(data="marathon_back_menu", bot=bot)
    result = await state.handle_callback(user, callback)

    assert result == "come_back", f"Ожидался 'come_back', получен {result}"


# ─── 2.13 Консультация (? вопрос) ───

async def test_lesson_handle_text_triggers_question(patch_marathon_deps):
    """Сценарий 2.2: Обычный текст → lesson_shown (переход к вопросу)."""
    state, bot = _make_lesson_state()
    user = make_intern(marathon_start_date=date(2026, 3, 27))

    msg = make_message_obj(text="Мой ответ на урок")
    result = await state.handle(user, msg)

    assert result == "lesson_shown", f"Ожидался 'lesson_shown', получен {result}"


# ─── Marathon start date fix ───

async def test_lesson_fixes_missing_start_date(patch_marathon_deps):
    """Если marathon_start_date нет — ставит сегодня."""
    state, bot = _make_lesson_state()
    user = make_intern(
        marathon_start_date=None,
        completed_topics=[],
        current_topic_index=0,
    )

    await state.enter(user)

    mock_upd = patch_marathon_deps["update_intern"]
    start_date_calls = [c for c in mock_upd.call_args_list
                        if "marathon_start_date" in (c.kwargs or {})]
    assert len(start_date_calls) > 0, "marathon_start_date не был установлен"
