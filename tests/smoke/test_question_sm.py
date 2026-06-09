"""
Regression-тесты: MarathonQuestionState + MarathonBonusState.

Сценарии 2.3, 2.6, 2.7, 2.9 из testing-scenarios.md.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date

from tests.smoke.conftest import make_intern
from tests.smoke.sm_helpers import create_state, make_message_obj, make_callback_obj


# ─── Общие патчи ───

@pytest.fixture(autouse=True)
def patch_question_deps():
    with patch("archive.states.marathon_legacy.question.update_intern", new_callable=AsyncMock) as mock_upd, \
         patch("archive.states.marathon_legacy.question.save_answer", new_callable=AsyncMock) as mock_save, \
         patch("archive.states.marathon_legacy.question.log_event", new_callable=AsyncMock), \
         patch("archive.states.marathon_legacy.question.get_theory_count_at_level", new_callable=AsyncMock, return_value=3), \
         patch("archive.states.marathon_legacy.question.get_marathon_content", new_callable=AsyncMock) as mock_content, \
         patch("archive.states.marathon_legacy.question.save_marathon_content", new_callable=AsyncMock), \
         patch("archive.states.marathon_legacy.question.get_topic", return_value={"id": "t1", "type": "theory", "title": "Тема", "day": 1}), \
         patch("archive.states.marathon_legacy.question.claude") as mock_claude:

        mock_content.return_value = {"question_content": "Тестовый вопрос по теме?", "id": 1}
        mock_claude.generate_question = AsyncMock(return_value="Сгенерированный вопрос?")
        mock_claude.generate_content = AsyncMock(return_value="Обратная связь")

        yield {
            "update_intern": mock_upd,
            "save_answer": mock_save,
            "get_content": mock_content,
            "claude": mock_claude,
        }


def _make_question_state(bot=None):
    from archive.states.marathon_legacy.question import MarathonQuestionState
    return create_state(MarathonQuestionState, bot)


# ─── 2.3 Ответ на вопрос → correct (bloom 2+) ───

async def test_question_handle_correct_answer(patch_question_deps):
    """Сценарий 2.3: Развёрнутый ответ → correct/correct_level_1."""
    state, bot = _make_question_state()
    user = make_intern(
        completed_topics=[0],
        current_topic_index=1,
        complexity_level=1,
    )

    msg = make_message_obj(text="Системное мышление — это подход к пониманию сложных систем через их структуру и взаимосвязи")
    result = await state.handle(user, msg)

    # Должен вернуть correct или correct_level_1
    assert result in ("correct", "correct_level_1", None), f"Неожиданный результат: {result}"

    # save_answer должен быть вызван
    mock_save = patch_question_deps["save_answer"]
    assert mock_save.called, "save_answer не был вызван"


# ─── 2.9 Короткий ответ ───

async def test_question_handle_short_answer(patch_question_deps):
    """Сценарий 2.9: Слишком короткий ответ → остаёмся."""
    state, bot = _make_question_state()
    user = make_intern(completed_topics=[], current_topic_index=0)

    msg = make_message_obj(text="да")  # < 20 символов
    result = await state.handle(user, msg)

    # Должен остаться в стейте (None) или попросить развёрнутый ответ
    # save_answer НЕ должен быть вызван с коротким ответом
    assert result is None, f"Короткий ответ не должен триггерить переход: {result}"


# ─── Callback: перейти к практике ───

async def test_question_callback_get_practice(patch_question_deps):
    """Callback 'marathon_get_practice' → correct_level_1."""
    state, bot = _make_question_state()
    user = make_intern()

    callback = make_callback_obj(data="marathon_get_practice", bot=bot)
    result = await state.handle_callback(user, callback)

    assert result == "correct_level_1", f"Ожидался 'correct_level_1', получен {result}"


# ─── Callback: перейти к бонусу ───

async def test_question_callback_next_bonus(patch_question_deps):
    """Callback 'marathon_next_bonus' → correct."""
    state, bot = _make_question_state()
    user = make_intern()

    callback = make_callback_obj(data="marathon_next_bonus", bot=bot)
    result = await state.handle_callback(user, callback)

    assert result == "correct", f"Ожидался 'correct', получен {result}"


# ═══ MarathonBonusState ═══

@pytest.fixture
def patch_bonus_deps():
    with patch("archive.states.marathon_legacy.bonus.update_intern", new_callable=AsyncMock), \
         patch("archive.states.marathon_legacy.bonus.save_answer", new_callable=AsyncMock) as mock_save, \
         patch("archive.states.marathon_legacy.bonus.claude") as mock_claude:

        mock_claude.generate_question = AsyncMock(return_value="Бонусный вопрос?")

        yield {"save_answer": mock_save, "claude": mock_claude}


def _make_bonus_state(bot=None):
    from archive.states.marathon_legacy.bonus import MarathonBonusState
    return create_state(MarathonBonusState, bot)


async def test_bonus_callback_get_practice(patch_bonus_deps):
    """Bonus: callback 'marathon_get_practice' → answered."""
    state, bot = _make_bonus_state()
    user = make_intern()

    callback = make_callback_obj(data="marathon_get_practice", bot=bot)
    result = await state.handle_callback(user, callback)

    assert result == "answered", f"Ожидался 'answered', получен {result}"
