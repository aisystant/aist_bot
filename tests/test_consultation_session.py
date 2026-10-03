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
from states.common.consultation import (  # noqa: E402
    MAX_HISTORY_PAIRS,
    SESSION_TIMEOUT_SEC,
    ConsultationState,
    _head_tail,
)

LOGGER_NAME = "states.common.consultation"
CONSULTATION = "common.consultation"
LONG_ANSWER = "Роль отвечает за приём заявок и их распределение между исполнителями. " * 12  # > 400 символов
PAIR = {"q": "опиши роль", "a": LONG_ANSWER}
SHORT_ANSWER = "Роль принимает заявки и распределяет их между исполнителями."  # короче 400 символов: ветка ответа из FAQ
OFFER = "Хочешь — опишем, что конкретно делает эта роль сейчас?"


def long_text(words: int, ending: str = "") -> str:
    return " ".join(f"слово{i}" for i in range(words)) + ending


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
async def test_failed_cleanup_save_after_the_window_does_not_lose_the_reply():
    # State Machine глушит исключение стейта: без перехвата реплика читателя пропала бы молча
    state = make_state()
    ctx = {
        "consultation_history": [{"q": "старый вопрос", "a": "старый ответ"}],
        "consultation_last_activity": time.time() - 16 * 60,
    }
    user = make_user(1006, ctx)

    with patch.object(state, "enter", new=AsyncMock(return_value=None)) as enter, \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=RuntimeError("база недоступна"))):
        event = await state.handle(user, SimpleNamespace(text="новая тема разговора"))

    assert event == "followup"
    enter.assert_awaited_once_with(user, context={"question": "новая тема разговора"})
    assert "consultation_history" not in ctx  # очистка есть в памяти и уйдёт в базу с записью после ответа


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
    first, second = db.ctx["consultation_history"]
    assert first["q"] == PAIR["q"]
    assert first["a"].startswith(LONG_ANSWER[:100]) and first["a"].endswith(LONG_ANSWER[-100:])  # пара стала старой: голова и хвост
    assert second == {"q": PAIR["q"], "a": "Подробный ответ"}
    messages = model.await_args.kwargs["conversation_messages"]
    assert messages == [
        {"role": "user", "content": PAIR["q"]},
        {"role": "assistant", "content": LONG_ANSWER},
        {"role": "user", "content": PAIR["q"]},
    ]


# Две ветки указания «подробнее»: длинный ответ (раскрыть не затронутое) и короткий, из FAQ (ответить точно)
REFINE_INSTRUCTIONS = [
    pytest.param(LONG_ANSWER, "Раскрой аспекты, которые не были затронуты выше", id="long-answer"),
    pytest.param(SHORT_ANSWER, "Дай конкретный практический ответ на его вопрос", id="short-faq-answer"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("previous, instruction", REFINE_INSTRUCTIONS)
async def test_refine_does_not_quote_answer_that_is_already_last_in_history(previous, instruction):
    state = make_state()
    db = FakeContextDb(2004, {"consultation_history": [{"q": PAIR["q"], "a": previous}], "consultation_last_activity": time.time()})
    refine = {"question": PAIR["q"], "refinement": True, "previous_answer": previous, "refinement_round": 2}

    with machine_over(state, db) as sm, model_call_patched(state, "Подробный ответ") as model:
        await sm.go_to(db.user(), CONSULTATION, context=refine)

    bot_context = model.await_args.kwargs["bot_context"]
    assert previous[:40] not in bot_context
    assert "выше в диалоге" in bot_context
    assert instruction in bot_context  # указание «подробнее» осталось


@pytest.mark.asyncio
@pytest.mark.parametrize("previous, instruction", REFINE_INSTRUCTIONS)
async def test_refine_quotes_answer_when_dialog_does_not_hold_it(previous, instruction):
    state = make_state()
    db = FakeContextDb(2005, {})  # история пуста: кнопку нажали под ответом, которого в сессии уже нет
    refine = {"question": PAIR["q"], "refinement": True, "previous_answer": previous, "refinement_round": 2}

    with machine_over(state, db) as sm, model_call_patched(state, "Подробный ответ") as model:
        await sm.go_to(db.user(), CONSULTATION, context=refine)

    bot_context = model.await_args.kwargs["bot_context"]
    assert previous[:40] in bot_context
    assert "выше в диалоге" not in bot_context
    assert instruction in bot_context


@pytest.mark.asyncio
async def test_refine_drops_pending_comment_even_if_the_model_call_fails():
    state = make_state()
    db = FakeContextDb(2014, live_session(qa_comment_id=7))
    refine = {"question": PAIR["q"], "refinement": True, "previous_answer": LONG_ANSWER, "refinement_round": 2}

    with machine_over(state, db) as sm, model_call_patched(state, "Подробный ответ") as model:
        model.side_effect = RuntimeError("модель недоступна")
        await sm.go_to(db.user(), CONSULTATION, context=refine)

    # ответ не получился и история не дописалась, но ожидание замечания уже снято и записано
    assert "qa_comment_id" not in db.ctx
    assert db.ctx["consultation_history"] == [PAIR]


@pytest.mark.asyncio
async def test_explicit_question_drops_pending_comment():
    state = make_state()
    db = FakeContextDb(2015, live_session(qa_comment_id=7))

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         model_call_patched(state, "Ответ"):
        await state.enter(db.user(), context={"question": "как тренировать внимание"})

    assert "qa_comment_id" not in db.ctx  # следующий обычный текст пойдёт в диалог, а не в замечание
    assert len(db.ctx["consultation_history"]) == 2


@pytest.mark.asyncio
async def test_feedback_button_extends_the_window():
    state = make_state()
    ten_minutes_ago = time.time() - 10 * 60
    db = FakeContextDb(2017, live_session(consultation_last_activity=ten_minutes_ago))

    with machine_over(state, db) as sm, \
         patch("core.access.access_layer.has_access", new=AsyncMock(return_value=True)):
        await sm.go_to(db.user(), CONSULTATION, context={"comment_mode": True, "comment_qa_id": 7})

    assert time.time() - db.ctx["consultation_last_activity"] < 5  # окно считается от нажатия, а не от ответа


@pytest.mark.asyncio
async def test_saved_comment_extends_the_window():
    state = make_state()
    ten_minutes_ago = time.time() - 10 * 60
    db = FakeContextDb(2018, live_session(qa_comment_id=7, consultation_last_activity=ten_minutes_ago))

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         patch("db.queries.qa.update_qa_comment", new=AsyncMock()), \
         patch("core.feedback_triage.triage_feedback", new=AsyncMock()):
        await state.handle(db.user(), SimpleNamespace(text="ответ получился слишком общим"))
        await asyncio.sleep(0)

    assert time.time() - db.ctx["consultation_last_activity"] < 5


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


FULL_ANSWER = long_text(900, " " + OFFER)  # около 7 тысяч символов: в истории хранится как голова + хвост


@pytest.mark.parametrize("stored, previous, expected", [
    (LONG_ANSWER, LONG_ANSWER, True),                                          # тот же ответ
    (LONG_ANSWER + "\n\n_подпись роли_", LONG_ANSWER, True),                   # в истории с подписью роли в хвосте
    (_head_tail(FULL_ANSWER, 3000, 1000), FULL_ANSWER, True),                  # в истории сжат до головы и хвоста
    (long_text(80)[:-30] + " другой конец ответа, вот так", long_text(80), False),  # то же начало, другой конец
    ("B" * 500, "A" * 500, False),                                             # другой ответ
    ("", LONG_ANSWER, False),                                                  # в истории пустой ответ
    (LONG_ANSWER, "", False),                                                  # нечего сравнивать
], ids=["same", "role-signature-tail", "compacted", "same-start-other-end", "different", "empty-stored", "empty-previous"])
def test_is_last_answer_in_history(stored, previous, expected):
    ctx = {"consultation_history": [{"q": "q", "a": stored}]}

    assert ConsultationState._is_last_answer_in_history(ctx, previous) is expected


def test_is_last_answer_in_history_false_without_history():
    assert ConsultationState._is_last_answer_in_history({}, "A" * 500) is False


# =============================================================================
# Лента → консультация: темы недели доходят до модели
# =============================================================================

@pytest.mark.asyncio
async def test_feed_topics_live_until_the_session_ends():
    state = make_state()
    db = FakeContextDb(2007, {})

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         model_call_patched(state, "Ответ") as model:
        user = {**db.user(), "current_topic": "Тема марафона"}
        await state.enter(user, context={"question": "как устроена оперативная память", "context_topic": "Внимание, Собранность"})
        assert model.await_args.kwargs["context_topic"] == "Внимание, Собранность"

        user = {**db.user(), "current_topic": "Тема марафона"}  # реплика без тем: их помнит сессия
        await state.enter(user, context={"question": "а как её тренировать"})
        assert model.await_args.kwargs["context_topic"] == "Внимание, Собранность"

    state._clear_session(db.ctx, reason="exit")
    assert "consultation_topic" not in db.ctx  # с концом сессии темы уходят


@pytest.mark.asyncio
async def test_feed_topics_are_kept_when_the_first_answer_is_a_fast_bot_answer():
    state = make_state()
    db = FakeContextDb(2019, {})

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         patch("states.common.consultation.save_qa", new=AsyncMock(return_value=None)), \
         model_call_patched(state, "Ответ") as model:
        # первый вопрос из Ленты - о боте: отвечает быстрый путь, модель не зовётся
        await state.enter(db.user(), context={"question": "что ты умеешь?", "context_topic": "Внимание, Собранность"})
        model.assert_not_awaited()

        await state.enter(db.user(), context={"question": "как тренировать внимание"})
        assert model.await_args.kwargs["context_topic"] == "Внимание, Собранность"


@pytest.mark.asyncio
async def test_marathon_topic_is_used_when_the_session_has_no_feed_topics():
    state = make_state()
    db = FakeContextDb(2016, {})

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         model_call_patched(state, "Ответ") as model:
        user = {**db.user(), "current_topic": "Тема марафона"}
        await state.enter(user, context={"question": "как устроена оперативная память"})

    assert model.await_args.kwargs["context_topic"] == "Тема марафона"
    assert "consultation_topic" not in db.ctx


# =============================================================================
# Усечение истории «голова + хвост»: последняя пара почти целиком, в хвосте ответа - встречный вопрос
# =============================================================================



def test_head_tail_returns_short_text_unchanged():
    assert _head_tail("коротко", 10, 10) == "коротко"
    assert _head_tail("x" * 20, 10, 10) == "x" * 20  # ровно по границе


def test_head_tail_keeps_both_ends_and_reports_omitted_count():
    text = long_text(2000, " " + OFFER)

    result = _head_tail(text, 3000, 1000)

    assert result.startswith("слово0 слово1 ")
    assert result.endswith(OFFER)
    omitted = int(result.split("[…пропущено ")[1].split(" символов…]")[0])
    head, tail = result.split(f"\n[…пропущено {omitted} символов…]\n")
    assert omitted == len(text) - len(head) - len(tail)
    assert text.startswith(head) and text.endswith(tail)


def ragged_text(words: int) -> str:
    """Слова разной длины: места реза попадают в разные позиции внутри слов."""
    return " ".join("б" * (3 + i % 7) + str(i) for i in range(words))


@pytest.mark.parametrize("words", range(1000, 1010))  # соседние длины сдвигают места реза
def test_head_tail_cuts_on_word_boundaries(words):
    text = ragged_text(words)

    head, rest = _head_tail(text, 3000, 1000).split("\n[…пропущено ")
    tail = rest.split("…]\n")[1]

    assert head.split()[-1] in text.split()   # последнее слово головы целое
    assert tail.split()[0] in text.split()    # первое слово хвоста целое


def test_head_tail_is_fast_when_a_long_word_precedes_the_cut():
    # Длинное слово без пробелов в начале головы, рез пришёлся на середину другого слова: прежняя регулярка
    # перебирала каждую позицию внутри длинного слова (около 0,2-0,5 с на 3000 символов, цикл событий стоял)
    text = "я" * 2400 + " " + "слово " * 300

    started = time.perf_counter()
    result = _head_tail(text, 2500, 500)
    elapsed = time.perf_counter() - started

    assert result.startswith("я" * 2400 + " слово")
    assert elapsed < 0.05


def test_head_tail_hard_cut_when_text_has_no_spaces():
    result = _head_tail("x" * 100, 20, 10)

    assert result.startswith("x" * 20 + "\n[…пропущено 70 символов…]\n")
    assert result.endswith("x" * 10)


def test_last_pair_keeps_closing_offer_of_long_answer():
    state = make_state()
    answer = long_text(250, " " + OFFER)  # около 2000 символов
    assert 1500 < len(answer) <= 4000  # лимит последней пары: голова 3000 + хвост 1000

    ctx = state._append_history({}, "опиши роль", answer)

    assert ctx["consultation_history"][-1]["a"] == answer  # целиком, без маркера
    messages = state._build_history_messages(ctx, "опиши")
    assert messages[-2]["content"].endswith(OFFER)


def test_previous_pair_is_compacted_when_a_new_one_arrives():
    state = make_state()
    first_answer = long_text(800, " " + OFFER)  # заведомо длиннее лимита старой пары
    first_question = long_text(200)

    ctx = state._append_history({}, first_question, first_answer)
    state._append_history(ctx, "опиши", "Ответ на второй вопрос")
    first, second = ctx["consultation_history"]

    assert second == {"q": "опиши", "a": "Ответ на второй вопрос"}
    assert first["a"].startswith(first_answer[:100]) and first["a"].endswith(OFFER)
    assert "[…пропущено " in first["a"] and "[…пропущено " in first["q"]
    assert len(first["a"]) <= 660  # старая пара: голова 300 + хвост 300 + маркер
    assert len(first["q"]) <= 510  # голова 300 + хвост 150 + маркер


def test_history_size_is_bounded_for_huge_dialogs():
    state = make_state()
    ctx: dict = {}

    for i in range(MAX_HISTORY_PAIRS + 3):
        state._append_history(ctx, long_text(3000), long_text(3000, f" конец{i}"))
    history = ctx["consultation_history"]

    assert len(history) == MAX_HISTORY_PAIRS
    assert history[-1]["a"].endswith(f"конец{MAX_HISTORY_PAIRS + 2}")
    total = sum(len(p["q"]) + len(p["a"]) for p in history)
    assert total <= 12_000  # измеренный потолок пяти пар около 11,4 тысячи символов


@pytest.mark.asyncio
async def test_history_keeps_answer_without_role_signature_and_model_sees_the_offer():
    from engines.shared.consultation_tools import get_role_footer

    state = make_state()
    db = FakeContextDb(2008, {})
    answer = long_text(250, " " + OFFER)

    with patch.object(state, "send", new=AsyncMock()) as send, \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         model_call_patched(state, answer) as model:
        await state.enter(db.user(), context={"question": "Навигатор, с чего начать?"})  # явное имя роли (Ф14: без имени вопрос идёт к Наставнику): к ответу добавится подпись роли
        await state.enter(db.user(), context={"question": "опиши"})

    footer = get_role_footer("navigator", "ru")
    shown = " ".join(call.args[1] for call in send.await_args_list)
    assert footer.splitlines()[0] in shown             # читатель подпись видит
    stored = db.ctx["consultation_history"][0]["a"]
    assert footer.splitlines()[0] not in stored         # в истории её нет
    assert stored.endswith(OFFER)
    messages = model.await_args.kwargs["conversation_messages"]
    assert messages[-2]["content"].endswith(OFFER)      # модель видит встречное предложение, а не подпись


@pytest.mark.asyncio
async def test_refine_quotes_long_previous_answer_head_and_tail():
    state = make_state()
    db = FakeContextDb(2009, {})  # история пуста: цитата нужна
    previous = long_text(1500, " " + OFFER)  # около 11 тысяч символов
    refine = {"question": "q", "refinement": True, "previous_answer": previous, "refinement_round": 2}

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)), \
         model_call_patched(state, "Подробный ответ") as model:
        await state.enter(db.user(), context=refine)

    bot_context = model.await_args.kwargs["bot_context"]
    assert previous[:200] in bot_context
    assert OFFER in bot_context          # хвост предыдущего ответа не потерян
    assert "[…пропущено " in bot_context


# =============================================================================
# Короткие реплики в живом диалоге («да», «ок») продолжают его, а не считаются случайным вводом
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["да", "ок", "а?", "2"])
async def test_short_reply_in_live_dialog_continues_the_dialog(text):
    state = make_state()
    user = FakeContextDb(2010, live_session()).user()

    with patch.object(state, "enter", new=AsyncMock(return_value=None)) as enter, \
         patch.object(state, "send", new=AsyncMock()) as send:
        event = await state.handle(user, SimpleNamespace(text=text))

    assert event == "followup"
    enter.assert_awaited_once_with(user, context={"question": text})
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_text_in_empty_session_still_gets_the_hint():
    from i18n import t

    state = make_state()
    user = FakeContextDb(2011, {}).user()

    with patch.object(state, "enter", new=AsyncMock()) as enter, \
         patch.object(state, "send", new=AsyncMock()) as send:
        event = await state.handle(user, SimpleNamespace(text="да"))

    assert event is None
    enter.assert_not_awaited()
    assert send.await_args.args[1] == t("consultation.session_hint", "ru")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["?", ".", "👍", "!!"])
async def test_reply_without_letters_or_digits_in_live_dialog_gets_the_hint(text):
    state = make_state()
    user = FakeContextDb(2012, live_session()).user()

    with patch.object(state, "enter", new=AsyncMock()) as enter, \
         patch.object(state, "send", new=AsyncMock()) as send:
        event = await state.handle(user, SimpleNamespace(text=text))

    assert event is None
    enter.assert_not_awaited()
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_short_reply_after_the_window_ends_the_session():
    state = make_state()
    expired = live_session(consultation_last_activity=time.time() - 16 * 60)
    db = FakeContextDb(2013, expired)

    with patch.object(state, "enter", new=AsyncMock()) as enter, \
         patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=db.save)):
        event = await state.handle(db.user(), SimpleNamespace(text="да"))

    assert event == "done"
    enter.assert_not_awaited()
    assert "consultation_history" not in db.ctx
