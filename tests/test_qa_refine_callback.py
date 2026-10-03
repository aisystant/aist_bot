"""Unit-тесты РП-498 Ф17: кнопка «🔍 Подробнее» - просьба углубить ответ, а не оценка «ответ не помог».

Раньше нажатие ставило qa_history.helpful=false и запускало разбор «не помог»: 12,5% ответов консультации
получали отрицательную оценку только за то, что читатель попросил подробнее. Теперь сигнал пишется отдельным
событием qa_refine, а 👍 по-прежнему ставит helpful=true.
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from handlers.callbacks import cb_qa_feedback  # noqa: E402

CHAT_ID = 12345
QA_ID = 77
NOW = datetime(2026, 10, 1, 10, 0, 0)
QA = {"id": QA_ID, "chat_id": CHAT_ID, "question": "опиши роль", "answer": "Ответ про роль", "created_at": NOW}
FOREIGN_QA = {**QA, "chat_id": CHAT_ID + 1, "answer": "Чужой ответ, который не должен попасть к другому читателю"}


def make_callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.message.chat.id = CHAT_ID
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def make_intern(current_state: str = "common.consultation") -> dict:
    return {"chat_id": CHAT_ID, "language": "ru", "current_state": current_state}


class Collaborators:
    """Подмены вокруг cb_qa_feedback: вызовы, которые тесты проверяют, отдаются как атрибуты."""

    def __init__(self, qa=QA, history=None, intern=None):
        self.dispatcher = MagicMock(is_sm_active=True, go_to=AsyncMock())
        self.helpful = AsyncMock()
        self.triage = AsyncMock()
        self.log_event = AsyncMock()
        self.patches = [
            patch("handlers.callbacks.get_intern", new=AsyncMock(return_value=intern or make_intern())),
            patch("handlers.get_dispatcher", return_value=self.dispatcher),
            patch("db.queries.qa.get_qa_by_id", new=AsyncMock(return_value=qa)),
            patch("db.queries.qa.get_qa_history", new=AsyncMock(return_value=history if history is not None else [QA])),
            patch("db.queries.qa.update_qa_helpful", new=self.helpful),
            patch("core.feedback_triage.triage_feedback", new=self.triage),
            patch("db.queries.events.log_event", new=self.log_event),
        ]

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


@pytest.mark.asyncio
async def test_refine_goes_to_consultation_and_is_not_a_not_helpful_mark():
    with Collaborators() as env:
        await cb_qa_feedback(make_callback(f"qa_refine_{QA_ID}"), AsyncMock())

    env.helpful.assert_not_awaited()
    env.triage.assert_not_called()
    env.log_event.assert_awaited_once_with(CHAT_ID, "qa_refine", {"qa_id": QA_ID, "refinement_round": 2})
    env.dispatcher.go_to.assert_awaited_once()
    args, kwargs = env.dispatcher.go_to.await_args
    assert args[1] == "common.consultation"
    assert kwargs["context"] == {
        "question": "опиши роль", "refinement": True, "previous_answer": "Ответ про роль", "refinement_round": 2,
    }


@pytest.mark.asyncio
async def test_second_refine_of_the_same_question_logs_round_three():
    earlier = {"id": QA_ID - 1, "question": QA["question"], "answer": "Первый ответ", "created_at": NOW - timedelta(seconds=60)}

    with Collaborators(history=[QA, earlier]) as env:
        await cb_qa_feedback(make_callback(f"qa_refine_{QA_ID}"), AsyncMock())

    env.log_event.assert_awaited_once_with(CHAT_ID, "qa_refine", {"qa_id": QA_ID, "refinement_round": 3})


@pytest.mark.asyncio
async def test_refine_of_unknown_answer_reports_error_and_logs_nothing():
    callback = make_callback("qa_refine_999")

    with Collaborators(qa=None) as env:
        await cb_qa_feedback(callback, AsyncMock())

    callback.message.answer.assert_awaited_once()
    env.dispatcher.go_to.assert_not_awaited()
    env.log_event.assert_not_awaited()
    env.helpful.assert_not_awaited()


@pytest.mark.asyncio
async def test_thumbs_up_still_marks_answer_helpful():
    with Collaborators(intern=make_intern("feed.digest")) as env:
        await cb_qa_feedback(make_callback(f"qa_helpful_{QA_ID}"), AsyncMock())

    env.helpful.assert_awaited_once_with(QA_ID, True)
    env.log_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_button_of_own_record_starts_comment_mode():
    with Collaborators() as env:
        await cb_qa_feedback(make_callback(f"qa_comment_{QA_ID}"), AsyncMock())

    env.dispatcher.go_to.assert_awaited_once()
    args, kwargs = env.dispatcher.go_to.await_args
    assert args[1] == "common.consultation"
    assert kwargs["context"] == {"comment_mode": True, "comment_qa_id": QA_ID}


# callback_data приходит от клиента: чужой номер записи не должен ни читаться, ни меняться
@pytest.mark.asyncio
@pytest.mark.parametrize("data", [f"qa_helpful_{QA_ID}", f"qa_refine_{QA_ID}", f"qa_comment_{QA_ID}"])
async def test_buttons_of_a_foreign_record_are_ignored(data):
    callback = make_callback(data)

    with Collaborators(qa=FOREIGN_QA) as env:
        await cb_qa_feedback(callback, AsyncMock())

    env.helpful.assert_not_awaited()         # оценка чужой записи не пишется
    env.log_event.assert_not_awaited()
    env.dispatcher.go_to.assert_not_awaited()  # ни чужой ответ в запрос, ни режим замечания к чужой записи
    for call in callback.message.answer.await_args_list:
        assert FOREIGN_QA["answer"] not in str(call)  # чужой текст не показывается
