"""РП-262 Ф10, выпуск А: нить хранит агент, бот не шлёт историю и при сбое агента не отвечает сам.

Покрывает engines.shared.interlocutor_client (классы исхода, повтор с тем же turn_id, DELETE нити,
замок по чату) и states.common.consultation (показ, отказ «недоступно», приглашение войти, «забыть»).
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from engines.shared import interlocutor_client as ic  # noqa: E402
from states.common import consultation as consultation_module  # noqa: E402
from states.common.consultation import ConsultationState  # noqa: E402

OWNER = 424242
URL = "https://agent.test/v1/turn"
PASS_VALUE = "person-pass-that-must-never-be-logged"


@pytest.fixture
def mode_on(monkeypatch):
    monkeypatch.setenv("INTERLOCUTOR_URL", URL)
    monkeypatch.setenv("INTERLOCUTOR_THREAD_MODE", "1")


@pytest.fixture(autouse=True)
def no_real_sleep_or_db():
    """Retry pause and counters must not touch the clock or a database."""
    with patch.object(ic, "RETRY_PAUSE_S", 0), patch(
        "db.queries.interlocutor_stats.bump", AsyncMock()
    ) as bump:
        yield bump


def _patch_token(token, connected=None):
    """A gateway double. `connected` = the person is signed in; by default signed in iff a pass exists."""
    gateway = MagicMock()
    gateway.access_token_for = AsyncMock(return_value=token)
    gateway.is_connected = MagicMock(return_value=bool(token) if connected is None else connected)
    return patch("clients.gateway_mcp.gateway_mcp", gateway)


def make_state():
    state = ConsultationState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
    state.send = AsyncMock()
    state._keep_typing = MagicMock(return_value=MagicMock(cancel=MagicMock()))
    state._save_session_context = AsyncMock()
    state._get_mode = MagicMock(return_value="marathon")
    return state


def answer(text="ответ агента"):
    return {"outcome": "answer", "text": text, "sources": []}


# ---- флаг и ключ повтора


def test_thread_mode_is_on_only_for_one(monkeypatch):
    monkeypatch.delenv("INTERLOCUTOR_THREAD_MODE", raising=False)
    assert ic.thread_mode_enabled() is False
    for value in ("0", "true", "", "  "):
        monkeypatch.setenv("INTERLOCUTOR_THREAD_MODE", value)
        assert ic.thread_mode_enabled() is False
    monkeypatch.setenv("INTERLOCUTOR_THREAD_MODE", " 1 ")
    assert ic.thread_mode_enabled() is True


def test_one_telegram_message_gives_one_turn_id():
    assert ic.make_turn_id(OWNER, "m77") == f"{OWNER}:m77" == ic.make_turn_id(OWNER, "m77")
    assert ic.make_turn_id(OWNER, "c5") != ic.make_turn_id(OWNER, "m77")
    assert ic.make_turn_id(OWNER).startswith(f"{OWNER}:")


def test_chat_lock_is_one_per_person():
    assert ic.chat_lock(OWNER) is ic.chat_lock(OWNER)
    assert ic.chat_lock(OWNER) is not ic.chat_lock(OWNER + 1)


# ---- классы исхода


@pytest.mark.parametrize(
    "reply, kind",
    [
        (answer(), ic.KIND_ANSWER),
        ({"outcome": "answer", "text": "  "}, ic.KIND_ERROR),
        ({"outcome": "need_login", "text": "войдите"}, ic.KIND_NO_PASS),
        ({"outcome": "tool_timeout", "text": "не успел"}, ic.KIND_RETRIABLE),
        ({"outcome": "source_unavailable", "text": "нет доступа"}, ic.KIND_RETRIABLE),
        (
            {"outcome": "source_unavailable", "text": "ключ отклонён", "fallback_allowed": False},
            ic.KIND_AGENT_MESSAGE,
        ),
        ({"outcome": "need_confirmation", "text": "подтвердите"}, ic.KIND_ERROR),
        (None, ic.KIND_ERROR),
    ],
)
async def test_agent_reply_is_classified(mode_on, reply, kind):
    post = AsyncMock(return_value=reply)
    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", post):
        result = await ic.turn(OWNER, "q", None, "ru", "t1", send_history=False)
    assert result.kind == kind


@pytest.mark.parametrize(
    "failure, kind",
    [
        (ic.HttpStatusError(500), ic.KIND_RETRIABLE),
        (ic.HttpStatusError(503), ic.KIND_RETRIABLE),
        (ic.HttpStatusError(429), ic.KIND_RETRIABLE),
        (ic.HttpStatusError(409), ic.KIND_ERROR),
        (ic.HttpStatusError(400), ic.KIND_ERROR),
        (ic.HttpStatusError(401), ic.KIND_NO_PASS),
        (ic.HttpStatusError(403), ic.KIND_NO_PASS),
        (aiohttp.ClientConnectionError("down"), ic.KIND_RETRIABLE),
        (TimeoutError(), ic.KIND_RETRIABLE),
    ],
)
async def test_transport_and_status_failures_are_classified(mode_on, failure, kind):
    post = AsyncMock(side_effect=failure)
    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", post):
        result = await ic.turn(OWNER, "q", None, "ru", "t1", send_history=False)
    assert result.kind == kind


async def test_no_url_or_no_pass_is_not_a_call(monkeypatch):
    post = AsyncMock()
    monkeypatch.setenv("INTERLOCUTOR_URL", "")
    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", post):
        assert (await ic.turn(OWNER, "q", None, "ru", "t", send_history=False)).kind == ic.KIND_DISABLED
    monkeypatch.setenv("INTERLOCUTOR_URL", URL)
    with _patch_token(None), patch.object(ic, "_post_json", post):
        assert (await ic.turn(OWNER, "q", None, "ru", "t", send_history=False)).kind == ic.KIND_NO_PASS
    post.assert_not_awaited()


async def test_signed_in_person_with_unrefreshable_pass_is_a_temporary_failure(mode_on):
    post = AsyncMock()
    with _patch_token(None, connected=True), patch.object(ic, "_post_json", post):
        result = await ic.turn(OWNER, "q", None, "ru", "t", send_history=False)
    assert result.kind == ic.KIND_RETRIABLE  # not "no pass": the bot must not answer for the agent
    post.assert_not_awaited()


async def test_failing_pass_lookup_is_a_temporary_failure(mode_on):
    gateway = MagicMock()
    gateway.access_token_for = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("clients.gateway_mcp.gateway_mcp", gateway):
        assert (await ic.turn(OWNER, "q", None, "ru", "t", send_history=False)).kind == ic.KIND_RETRIABLE
        assert await ic.forget(OWNER) == "failed"


async def test_history_is_sent_only_when_asked_and_turn_id_is_the_given_one(mode_on):
    seen = []

    async def fake_post(url, token, payload, timeout_s):
        seen.append(payload)
        return answer()

    pairs = [{"q": "раньше", "a": "ответ"}]
    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", fake_post):
        await ic.turn(OWNER, "q", pairs, "ru", "chat:m5", send_history=False)
        await ic.turn(OWNER, "q", pairs, "ru", "chat:m5", send_history=True)
    assert "history" not in seen[0]
    assert [h["role"] for h in seen[1]["history"]] == ["user", "assistant"]
    assert seen[0]["turn_id"] == seen[1]["turn_id"] == "chat:m5"
    assert PASS_VALUE not in str(seen)


# ---- «забыть» у агента


@pytest.mark.parametrize(
    "reply, failure, expected",
    [
        ({"outcome": "forgotten"}, None, "forgotten"),
        ({"outcome": "need_login"}, None, "refused"),
        ({"outcome": "tool_timeout"}, None, "refused"),
        (None, ic.HttpStatusError(500), "failed"),
        (None, aiohttp.ClientConnectionError("down"), "failed"),
    ],
)
async def test_forget_reports_what_the_agent_really_did(mode_on, reply, failure, expected):
    delete = AsyncMock(return_value=reply, side_effect=failure)
    with _patch_token(PASS_VALUE), patch.object(ic, "_delete_json", delete):
        assert await ic.forget(OWNER) == expected
    assert delete.await_args.args[0] == "https://agent.test/v1/thread"
    assert delete.await_args.args[1] == PASS_VALUE


async def test_forget_without_pass_or_url_calls_nothing(monkeypatch):
    delete = AsyncMock()
    monkeypatch.setenv("INTERLOCUTOR_URL", URL)
    with _patch_token(None, connected=True), patch.object(ic, "_delete_json", delete):
        assert await ic.forget(OWNER) == "failed"  # signed in, pass unavailable: the thread may still exist
    with _patch_token(None, connected=False), patch.object(ic, "_delete_json", delete):
        assert await ic.forget(OWNER) == "skipped"
    monkeypatch.setenv("INTERLOCUTOR_URL", "")
    with _patch_token(PASS_VALUE), patch.object(ic, "_delete_json", delete):
        assert await ic.forget(OWNER) == "skipped"
    delete.assert_not_awaited()


# ---- состояние: ответ, сбой, вход, повтор


def _results(*items):
    return AsyncMock(side_effect=[ic.TurnResult(kind, data, 12) for kind, data in items])


async def _run(state, turn, ctx=None, notes=None, turn_ref="m10"):
    with patch.object(ic, "turn", turn), patch("db.queries.activity.record_active_day", AsyncMock()):
        handled = await state._answer_via_interlocutor(
            SimpleNamespace(), OWNER, "вопрос", {} if ctx is None else ctx, "ru",
            paid_access=True, turn_ref=turn_ref, notes=notes,
        )
        await asyncio.sleep(0)
    return handled


async def test_thread_mode_shows_the_answer_without_history_and_offers_to_forget(mode_on):
    state, ctx = make_state(), {"consultation_history": [{"q": "было", "a": "ответ"}]}
    turn = _results((ic.KIND_ANSWER, answer("ответ агента")))
    assert await _run(state, turn, ctx) is True
    call = turn.await_args
    assert call.kwargs["send_history"] is False and call.args[2] is None
    assert call.args[4] == f"{OWNER}:m10"
    last = state.send.await_args_list[-1]
    assert "ответ агента" in last.args[1]
    assert last.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "qa_forget"
    assert ctx["consultation_history"][-1]["q"] == "вопрос"  # the bot copy is still written (rollback)
    assert ctx["agent_thread_live"] is True  # the agent now holds this person's thread


async def test_two_temporary_failures_give_one_unavailable_message_and_one_turn_id(mode_on, no_real_sleep_or_db):
    state, ctx = make_state(), {}
    turn = _results((ic.KIND_RETRIABLE, None), (ic.KIND_RETRIABLE, None))
    assert await _run(state, turn, ctx) is True  # handled: the old path does not run
    assert turn.await_count == 2
    assert turn.await_args_list[0].args[4] == turn.await_args_list[1].args[4]
    shown = [c.args[1] for c in state.send.await_args_list]
    assert sum("недоступен" in text for text in shown) == 1
    assert "consultation_history" not in ctx
    state._save_session_context.assert_not_awaited()
    counted = [c.args[0] for c in no_real_sleep_or_db.await_args_list]
    assert counted.count("retry") == 1 and "unavailable" in counted


async def test_one_temporary_failure_then_an_answer_shows_one_answer(mode_on):
    state = make_state()
    turn = _results((ic.KIND_RETRIABLE, None), (ic.KIND_ANSWER, answer("со второй попытки")))
    assert await _run(state, turn) is True
    shown = [c.args[1] for c in state.send.await_args_list]
    assert sum("со второй попытки" in text for text in shown) == 1
    assert not any("недоступен" in text for text in shown)


@pytest.mark.parametrize("kind", [ic.KIND_ERROR])
async def test_an_error_class_is_not_retried(mode_on, kind):
    state = make_state()
    turn = _results((kind, None))
    assert await _run(state, turn) is True
    assert turn.await_count == 1
    assert any("недоступен" in c.args[1] for c in state.send.await_args_list)


async def test_an_agent_message_is_shown_once_and_not_retried_or_remembered(mode_on):
    state, ctx = make_state(), {}
    message = {"outcome": "source_unavailable", "text": "ваш ключ отклонён", "fallback_allowed": False}
    turn = _results((ic.KIND_AGENT_MESSAGE, message))
    assert await _run(state, turn, ctx) is True
    assert turn.await_count == 1
    assert "ваш ключ отклонён" in state.send.await_args_list[-1].args[1]
    assert state.send.await_args_list[-1].kwargs.get("reply_markup") is None
    assert "consultation_history" not in ctx


async def test_without_a_pass_the_old_path_runs_and_the_person_is_invited_to_sign_in(mode_on):
    state, notes = make_state(), {}
    turn = _results((ic.KIND_NO_PASS, None))
    assert await _run(state, turn, {}, notes) is False
    assert notes == {"login_invite": True}
    assert turn.await_count == 1  # no retry without a pass


async def test_switched_off_thread_mode_keeps_the_old_call_with_history(monkeypatch):
    monkeypatch.setenv("INTERLOCUTOR_URL", URL)
    monkeypatch.delenv("INTERLOCUTOR_THREAD_MODE", raising=False)
    state = make_state()
    turn, ask = AsyncMock(), AsyncMock(return_value=answer())
    with patch.object(ic, "turn", turn), patch.object(ic, "ask", ask), patch(
        "db.queries.activity.record_active_day", AsyncMock()
    ):
        assert await state._answer_via_interlocutor(
            SimpleNamespace(), OWNER, "вопрос", {"consultation_history": [{"q": "a", "a": "b"}]}, "ru",
            paid_access=True,
        ) is True
        await asyncio.sleep(0)
    turn.assert_not_awaited()
    assert ask.await_args.args[2][0] == {"q": "a", "a": "b"}  # history still goes to the agent


async def test_free_roles_stay_on_the_old_path_in_thread_mode(mode_on):
    state, turn = make_state(), AsyncMock()
    with patch.object(ic, "turn", turn):
        assert await state._answer_via_interlocutor(
            SimpleNamespace(), OWNER, "вопрос", {}, "ru", paid_access=False
        ) is False
    turn.assert_not_awaited()


# ---- конец разговора стирает нить


async def test_ending_the_session_erases_the_thread_only_when_there_was_a_conversation(mode_on):
    state = make_state()
    forget = AsyncMock(return_value="forgotten")
    with patch.object(ic, "forget", forget):
        person = SimpleNamespace(chat_id=OWNER)
        ctx = {"consultation_history": [{"q": "a", "a": "b"}], "agent_thread_live": True}
        await state._end_session(person, ctx, "ru")
        assert forget.await_count == 1
        assert "agent_thread_live" not in ctx and "agent_thread_dirty" not in ctx
        await state._end_session(person, {}, "ru")
        assert forget.await_count == 1  # nothing was asked: nothing to erase
        old_path_only = {"consultation_history": [{"q": "a", "a": "b"}]}  # the bot answered itself
        await state._end_session(person, old_path_only, "ru")
        assert forget.await_count == 1  # the agent never held a thread of this person


async def test_timeout_and_exit_erase_the_thread(mode_on):
    state = make_state()
    forget = AsyncMock(return_value="forgotten")
    ctx = {"consultation_history": [{"q": "a", "a": "b"}], "agent_thread_live": True}
    with patch.object(ic, "forget", forget):
        await state._expire_session(ctx, OWNER)
        assert forget.await_count == 1 and "consultation_history" not in ctx
        state._load_session_context = AsyncMock(return_value={"agent_thread_live": True})
        await state.exit({"chat_id": OWNER})
        assert forget.await_count == 2


async def test_a_failed_forget_never_blocks_leaving(mode_on, no_real_sleep_or_db):
    state = make_state()
    ctx = {"consultation_history": [{"q": "a", "a": "b"}], "agent_thread_live": True}
    with patch.object(ic, "forget", AsyncMock(side_effect=RuntimeError("boom"))):
        await state._end_session(SimpleNamespace(chat_id=OWNER), ctx, "ru")
    assert "consultation_history" not in ctx
    assert ctx["agent_thread_dirty"] is True  # remembered: the next question retries the erase
    state.send.assert_awaited()  # the farewell is still shown


@pytest.mark.parametrize("outcome", ["failed", "refused"])
async def test_a_failed_erase_is_retried_before_the_next_question(mode_on, outcome):
    state = make_state()
    ctx = {"agent_thread_live": True, "consultation_history": [{"q": "a", "a": "b"}]}
    with patch.object(ic, "forget", AsyncMock(return_value=outcome)):
        await state._expire_session(ctx, OWNER)
    assert ctx["agent_thread_dirty"] is True and "agent_thread_live" not in ctx
    order = []

    async def fake_forget(chat_id):
        order.append("forget")
        return "forgotten"

    async def fake_turn(*args, **kwargs):
        order.append("turn")
        return ic.TurnResult(ic.KIND_ANSWER, answer(), 3)

    with patch.object(ic, "forget", fake_forget), patch.object(ic, "turn", fake_turn), patch(
        "db.queries.activity.record_active_day", AsyncMock()
    ):
        assert await state._answer_via_interlocutor(SimpleNamespace(), OWNER, "q", ctx, "ru", paid_access=True)
        await asyncio.sleep(0)
    assert order == ["forget", "turn"]  # the old thread is erased first, then the new question
    assert "agent_thread_dirty" not in ctx and ctx["agent_thread_live"] is True


async def test_nothing_is_erased_when_thread_mode_is_off(monkeypatch):
    monkeypatch.setenv("INTERLOCUTOR_URL", URL)
    monkeypatch.delenv("INTERLOCUTOR_THREAD_MODE", raising=False)
    state, forget = make_state(), AsyncMock()
    with patch.object(ic, "forget", forget):
        await state._end_session(SimpleNamespace(chat_id=OWNER), {"consultation_history": [{"q": "a", "a": "b"}]}, "ru")
    forget.assert_not_awaited()


async def test_forget_waits_for_the_turn_in_progress_of_the_same_person(mode_on):
    events = []
    release = asyncio.Event()

    async def slow_turn(*args, **kwargs):
        events.append("turn started")
        await release.wait()
        events.append("turn done")
        return ic.TurnResult(ic.KIND_ANSWER, answer(), 5)

    async def fake_forget(chat_id):
        events.append("forget")
        return "forgotten"

    state = make_state()
    with patch.object(ic, "turn", slow_turn), patch.object(ic, "forget", fake_forget), patch(
        "db.queries.activity.record_active_day", AsyncMock()
    ):
        turn_task = asyncio.create_task(
            state._answer_via_interlocutor(SimpleNamespace(), OWNER, "q", {}, "ru", paid_access=True)
        )
        await asyncio.sleep(0.01)
        forget_task = asyncio.create_task(
            state._forget_agent_thread(OWNER, {"agent_thread_live": True}, "exit")
        )
        await asyncio.sleep(0.01)
        assert events == ["turn started"]  # forget has not started: the turn holds the person's lock
        release.set()
        await asyncio.gather(turn_task, forget_task)
    assert events == ["turn started", "turn done", "forget"]


# ---- кнопка «Забыть»


@pytest.mark.parametrize(
    "outcome, marker",
    [("forgotten", "стёрт"), ("refused", "30 минут"), ("failed", "30 минут")],
)
async def test_forget_button_is_honest_about_the_outcome(mode_on, outcome, marker):
    saved = {}

    async def fake_update(chat_id, **fields):
        saved.update(fields)

    ctx = {"consultation_history": [{"q": "a", "a": "b"}], "force_role": "navigator", "agent_thread_live": True}
    with patch.object(ic, "forget", AsyncMock(return_value=outcome)), patch(
        "db.queries.users.get_intern", AsyncMock(return_value={"current_context": ctx})
    ), patch("db.queries.users.update_intern", fake_update):
        ok, text = await consultation_module.forget_conversation(OWNER, "ru")
    assert marker in text
    assert ok is (outcome == "forgotten")  # the button stays after a failure, so a retry is possible
    assert "consultation_history" not in saved["current_context"]  # the bot copy is cleared in every case
    assert saved["current_context"]["force_role"] == "navigator"
    assert saved["current_context"].get("agent_thread_dirty", False) is (outcome != "forgotten")


# ---- главное меню после /start


async def test_text_in_the_menu_goes_to_the_agent_only_in_thread_mode(monkeypatch):
    from states.common.mode_select import ModeSelectState

    state = ModeSelectState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
    state.enter = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.go_to = AsyncMock()
    message = SimpleNamespace(text="а какая последняя?", message_id=31)
    user = {"chat_id": OWNER}
    with patch("handlers.get_dispatcher", return_value=dispatcher):
        monkeypatch.delenv("INTERLOCUTOR_THREAD_MODE", raising=False)
        await state.handle(user, message)
        dispatcher.go_to.assert_not_awaited()
        state.enter.assert_awaited_once()

        state.enter.reset_mock()
        monkeypatch.setenv("INTERLOCUTOR_THREAD_MODE", "1")
        await state.handle(user, message)
        assert dispatcher.go_to.await_args.args[1] == "common.consultation"
        assert dispatcher.go_to.await_args.kwargs["context"] == {
            "question": "а какая последняя?", "turn_ref": "m31"
        }
        state.enter.assert_not_awaited()

        dispatcher.go_to.reset_mock()
        await state.handle(user, SimpleNamespace(text=" . ", message_id=32))  # no letter or digit
        dispatcher.go_to.assert_not_awaited()
        state.enter.assert_awaited_once()
