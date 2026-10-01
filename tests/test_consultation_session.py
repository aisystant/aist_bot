"""Unit-тесты РП-498 Ф17: память диалога консультации.

Покрывает states.common.consultation: окно неактивности 15 минут, явное сохранение после
очистки по таймауту, причина каждой очистки истории в логе, размер истории в строке enter().
До Ф17 тестов на историю консультации в боте не было вообще.
"""

import asyncio
import copy
import logging
import os
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from core.machine import StateMachine  # noqa: E402
from states.common.consultation import SESSION_TIMEOUT_SEC, ConsultationState  # noqa: E402

LOGGER_NAME = "states.common.consultation"
CONSULTATION = "common.consultation"
LONG_ANSWER = "Роль отвечает за приём заявок и их распределение между исполнителями. " * 12  # > 400 символов
PAIR = {"q": "опиши роль", "a": LONG_ANSWER}


def make_state() -> ConsultationState:
    return ConsultationState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())


def make_user(chat_id: int, ctx: dict) -> dict:
    return {"chat_id": chat_id, "current_context": ctx, "language": "ru"}


# =============================================================================
# Окно неактивности: 15 минут
# =============================================================================

def test_window_is_fifteen_minutes_and_matches_routing_table():
    from config.settings import SM_EXPECTING_REPLY_STATES

    assert SESSION_TIMEOUT_SEC == 900
    assert SM_EXPECTING_REPLY_STATES["common.consultation"] == 15


@pytest.mark.asyncio
async def test_followup_inside_window_keeps_history():
    state = make_state()
    history = [{"q": "опиши роль", "a": "Ответ. Хочешь, опишем, что делает эта роль?"}]
    ctx = {"consultation_history": history, "consultation_last_activity": time.time() - 14 * 60}
    user = make_user(1001, ctx)

    with patch.object(state, "enter", new=AsyncMock(return_value=None)) as enter, \
         patch.object(state, "_save_session_context", new=AsyncMock()) as save:
        event = await state.handle(user, SimpleNamespace(text="опиши"))

    assert event == "followup"
    enter.assert_awaited_once_with(user, context={"question": "опиши"})
    assert ctx["consultation_history"] == history  # 14 минут: история жива
    save.assert_not_awaited()                      # очистки не было, лишних записей нет


@pytest.mark.asyncio
async def test_followup_after_window_clears_history_and_saves_it_explicitly():
    state = make_state()
    ctx = {
        "consultation_history": [{"q": "старый вопрос", "a": "старый ответ"}],
        "consultation_last_activity": time.time() - 16 * 60,
        "force_role": "navigator",
    }
    user = make_user(1002, ctx)

    with patch.object(state, "enter", new=AsyncMock(return_value=None)) as enter, \
         patch.object(state, "_save_session_context", new=AsyncMock()) as save:
        event = await state.handle(user, SimpleNamespace(text="новая тема разговора"))

    assert event == "followup"
    enter.assert_awaited_once_with(user, context={"question": "новая тема разговора"})
    assert "consultation_history" not in ctx
    assert ctx["force_role"] == "navigator"  # роль из /navigator переживает таймаут
    save.assert_awaited_once()               # очистка сохранена явно, не только в общем объекте
    saved_chat_id, saved_ctx = save.await_args.args
    assert saved_chat_id == 1002
    assert "consultation_history" not in saved_ctx
    assert saved_ctx["force_role"] == "navigator"


# =============================================================================
# Причина очистки истории в логе
# =============================================================================

def test_clear_session_logs_reason_and_pairs(caplog):
    state = make_state()
    ctx = {
        "consultation_history": [{"q": "1", "a": "1"}, {"q": "2", "a": "2"}],
        "consultation_last_activity": 1.0,
        "qa_comment_id": 7,
        "active_free_role": "navigator",
    }

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        state._clear_session(ctx, reason="exit")

    assert "session cleared: reason=exit pairs=2" in caplog.text
    assert ctx == {}  # история, отметка активности, флаг замечания и роль сняты


def test_clear_session_is_silent_for_empty_session(caplog):
    state = make_state()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        state._clear_session({}, reason="exit")

    assert "session cleared" not in caplog.text


@pytest.mark.asyncio
async def test_exit_saves_cleared_context_and_logs_exit_reason(caplog):
    state = make_state()
    ctx = {"consultation_history": [{"q": "q", "a": "a"}], "consultation_last_activity": time.time()}
    user = make_user(1003, ctx)

    with patch.object(state, "_save_session_context", new=AsyncMock()) as save, \
         caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = await state.exit(user)

    assert result == {"consultation_complete": True}
    assert "session cleared: reason=exit pairs=1" in caplog.text
    saved_ctx = save.await_args.args[1]
    assert "consultation_history" not in saved_ctx


@pytest.mark.asyncio
async def test_end_session_logs_end_session_reason(caplog):
    state = make_state()
    ctx = {"consultation_history": [{"q": "q", "a": "a"}], "consultation_last_activity": time.time()}
    user = make_user(1004, ctx)

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock()), \
         caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await state._end_session(user, ctx, "ru")

    assert "session cleared: reason=end_session pairs=1" in caplog.text


# =============================================================================
# Строка enter(): размер истории
# =============================================================================

@pytest.mark.asyncio
async def test_enter_log_line_reports_history_size(caplog):
    state = make_state()
    history = [{"q": "abc", "a": "defgh"}]  # 8 символов
    user = make_user(1005, {"consultation_history": history})

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock()), \
         patch("core.access.access_layer.has_access", new=AsyncMock(return_value=True)), \
         caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await state.enter(user, context={"comment_mode": True, "comment_qa_id": 7})

    assert "history_pairs=1" in caplog.text
    assert "history_chars=8" in caplog.text


# =============================================================================
# Кнопки «Подробнее» / «Обратная связь»: память диалога через go_to() в то же состояние
# =============================================================================

class FakeContextDb:
    """Эмулирует хранение current_context: запись копирует, перечитывание отдаёт копию (как Postgres)."""

    def __init__(self, chat_id: int, ctx: dict):
        self.chat_id = chat_id
        self.ctx = copy.deepcopy(ctx)

    async def save(self, chat_id: int, ctx: dict) -> None:
        assert chat_id == self.chat_id
        self.ctx = copy.deepcopy(ctx)

    def user(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "current_state": CONSULTATION,
            "current_context": copy.deepcopy(self.ctx),
            "language": "ru",
        }


def live_session(**extra) -> dict:
    return {"consultation_history": [dict(PAIR)], "consultation_last_activity": time.time(), **extra}


@contextmanager
def machine_over(state: ConsultationState, db: FakeContextDb):
    """Настоящие StateMachine + ConsultationState; подменено только хранилище и отправка сообщений."""
    sm = StateMachine()
    sm.register(state)
    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         patch("db.queries.update_user_state", new=AsyncMock()), \
         patch("db.queries.get_intern", new=AsyncMock(side_effect=lambda chat_id: db.user())):
        yield sm


@contextmanager
def model_call_patched(state: ConsultationState, answer: str):
    """Подмена всего, что enter() зовёт вне состояния на пути к модели; отдаёт мок вызова модели."""
    model = AsyncMock(return_value=(answer, []))
    with patch.object(state, "_detect_tier", new=AsyncMock(return_value=(1, False, False))), \
         patch("states.common.consultation.get_self_knowledge", return_value="SELF"), \
         patch("states.common.consultation.match_faq", return_value=None), \
         patch("states.common.consultation.structured_lookup", return_value=None), \
         patch("states.common.consultation.get_latest_qa_id", new=AsyncMock(return_value=11)), \
         patch("core.access.access_layer.has_access", new=AsyncMock(return_value=True)), \
         patch("core.tier_detector.detect_ui_tier", new=AsyncMock(return_value=1)), \
         patch("engines.shared.handle_question_with_tools", new=model), \
         patch("db.queries.activity.record_active_day", new=AsyncMock()):
        yield model


@pytest.mark.asyncio
async def test_feedback_button_keeps_history_and_free_role_and_skips_paywall():
    state = make_state()
    db = FakeContextDb(2001, live_session(active_free_role="mentor"))
    has_access = AsyncMock(return_value=False)  # платный барьер закрыт: бесплатная роль должна его обойти

    with machine_over(state, db) as sm, patch("core.access.access_layer.has_access", new=has_access):
        await sm.go_to(db.user(), CONSULTATION, context={"comment_mode": True, "comment_qa_id": 7})

    assert db.ctx["consultation_history"] == [PAIR]
    assert db.ctx["qa_comment_id"] == 7
    assert db.ctx["active_free_role"] == "mentor"
    has_access.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_after_feedback_button_is_saved_as_comment_and_history_survives():
    state = make_state()
    db = FakeContextDb(2002, live_session(qa_comment_id=7))
    update_comment = AsyncMock()

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         patch("db.queries.qa.update_qa_comment", new=update_comment), \
         patch("core.feedback_triage.triage_feedback", new=AsyncMock()):
        event = await state.handle(db.user(), SimpleNamespace(text="ответ получился слишком общим"))
        await asyncio.sleep(0)  # дать отработать фоновой задаче разбора замечания

    assert event is None  # остаёмся в сессии
    update_comment.assert_awaited_once_with(7, "ответ получился слишком общим")
    assert "qa_comment_id" not in db.ctx
    assert db.ctx["consultation_history"] == [PAIR]


@pytest.mark.asyncio
async def test_refine_button_cancels_comment_mode_and_extends_dialog():
    state = make_state()
    db = FakeContextDb(2003, live_session(qa_comment_id=7))
    refine = {"question": PAIR["q"], "refinement": True, "previous_answer": LONG_ANSWER, "refinement_round": 2}

    with machine_over(state, db) as sm, model_call_patched(state, "Подробный ответ") as model:
        await sm.go_to(db.user(), CONSULTATION, context=refine)

    assert "qa_comment_id" not in db.ctx  # следующий текст пойдёт в диалог, а не в замечание
    assert db.ctx["consultation_history"] == [PAIR, {"q": PAIR["q"], "a": "Подробный ответ"}]
    messages = model.await_args.kwargs["conversation_messages"]
    assert messages == [
        {"role": "user", "content": PAIR["q"]},
        {"role": "assistant", "content": LONG_ANSWER},
        {"role": "user", "content": PAIR["q"]},
    ]


@pytest.mark.asyncio
async def test_refine_does_not_quote_answer_that_is_already_last_in_history():
    state = make_state()
    db = FakeContextDb(2004, live_session())
    refine = {"question": PAIR["q"], "refinement": True, "previous_answer": LONG_ANSWER, "refinement_round": 2}

    with machine_over(state, db) as sm, model_call_patched(state, "Подробный ответ") as model:
        await sm.go_to(db.user(), CONSULTATION, context=refine)

    bot_context = model.await_args.kwargs["bot_context"]
    assert LONG_ANSWER[:100] not in bot_context
    assert "выше в диалоге" in bot_context
    assert "Раскрой аспекты, которые не были затронуты выше" in bot_context  # указание «подробнее» осталось


@pytest.mark.asyncio
async def test_refine_quotes_answer_when_dialog_does_not_hold_it():
    state = make_state()
    db = FakeContextDb(2005, {})  # история пуста: кнопку нажали под ответом, которого в сессии уже нет
    refine = {"question": PAIR["q"], "refinement": True, "previous_answer": LONG_ANSWER, "refinement_round": 2}

    with machine_over(state, db) as sm, model_call_patched(state, "Подробный ответ") as model:
        await sm.go_to(db.user(), CONSULTATION, context=refine)

    bot_context = model.await_args.kwargs["bot_context"]
    assert LONG_ANSWER[:100] in bot_context
    assert "выше в диалоге" not in bot_context


@pytest.mark.asyncio
async def test_navigator_command_inside_session_still_starts_clean_session():
    state = make_state()
    db = FakeContextDb(2006, {**live_session(), "consultation_history": [dict(PAIR), dict(PAIR)]})

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)):
        await state.enter(db.user(), context={"force_role": "navigator"})

    assert "consultation_history" not in db.ctx
    assert db.ctx["force_role"] == "navigator"
    assert db.ctx["active_free_role"] == "navigator"


@pytest.mark.parametrize("stored, previous, expected", [
    ("A" * 500, "A" * 500, True),                       # тот же ответ
    ("A" * 500 + "\n\n_подпись роли_", "A" * 500, True),  # в истории с подписью роли в хвосте
    ("A" * 800, "A" * 2000, True),                      # в истории обрезан до 800 символов
    ("B" * 500, "A" * 500, False),                      # другой ответ
    ("", "A" * 500, False),                             # в истории пустой ответ
    ("A" * 500, "", False),                             # нечего сравнивать
], ids=["same", "role-signature-tail", "history-truncated", "different", "empty-stored", "empty-previous"])
def test_is_last_answer_in_history(stored, previous, expected):
    ctx = {"consultation_history": [{"q": "q", "a": stored}]}

    assert ConsultationState._is_last_answer_in_history(ctx, previous) is expected


def test_is_last_answer_in_history_false_without_history():
    assert ConsultationState._is_last_answer_in_history({}, "A" * 500) is False


# =============================================================================
# Лента → консультация: темы недели доходят до модели
# =============================================================================

@pytest.mark.asyncio
async def test_feed_topics_reach_the_model_and_marathon_topic_is_the_fallback():
    state = make_state()
    db = FakeContextDb(2007, {})

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         model_call_patched(state, "Ответ") as model:
        user = {**db.user(), "current_topic": "Тема марафона"}
        await state.enter(user, context={"question": "как устроена оперативная память", "context_topic": "Внимание, Собранность"})
        assert model.await_args.kwargs["context_topic"] == "Внимание, Собранность"

        user = {**db.user(), "current_topic": "Тема марафона"}
        await state.enter(user, context={"question": "а как её тренировать"})
        assert model.await_args.kwargs["context_topic"] == "Тема марафона"
