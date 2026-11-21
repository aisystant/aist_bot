"""РП-262 Ф10: нить у агента, классы отказов, повтор и удаление нити."""

import asyncio
import logging
import os
import sys
import time
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
from states.common.consultation import (  # noqa: E402
    DELETE_PENDING_KEY,
    PENDING_TURN_KEY,
    SESSION_TIMEOUT_SEC,
    ConsultationState,
)
from states.common.mode_select import ModeSelectState  # noqa: E402

OWNER = 424242
OTHER = 777
URL = "https://agent.test/v1/turn"
PASS_VALUE = "person-pass-that-must-never-be-logged"
ANSWER = {"outcome": "answer", "text": "ответ агента", "sources": []}


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("INTERLOCUTOR_URL", URL)


def _patch_token(token):
    gateway = MagicMock()
    gateway.access_token_for = AsyncMock(return_value=token)
    return patch("clients.gateway_mcp.gateway_mcp", gateway)


def make_state():
    state = ConsultationState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
    state.send = AsyncMock()
    state._save_session_context = AsyncMock()
    state._get_mode = MagicMock(return_value="marathon")
    return state


def test_flag_is_on_only_with_a_url(monkeypatch):
    monkeypatch.delenv("INTERLOCUTOR_URL", raising=False)
    assert ic.is_enabled() is False
    monkeypatch.setenv("INTERLOCUTOR_URL", "   ")
    assert ic.is_enabled() is False
    monkeypatch.setenv("INTERLOCUTOR_URL", URL)
    assert ic.is_enabled() is True


async def test_ask_sends_empty_history_and_the_given_turn_id(flag_on):
    seen = {}

    async def fake_post(url, token, payload, timeout_s):
        seen.update(url=url, token=token, payload=payload)
        return ANSWER

    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", fake_post):
        result = await ic.ask(OWNER, "вопрос", "ru", "turn-17")
    assert result["text"] == "ответ агента"
    assert seen["url"] == URL and seen["token"] == PASS_VALUE
    assert seen["payload"]["history"] == []
    assert seen["payload"]["turn_id"] == "turn-17"
    assert PASS_VALUE not in str(seen["payload"])


async def test_missing_pass_is_a_distinct_outcome_without_an_http_call(flag_on):
    with _patch_token(None), patch.object(ic, "_post_json", AsyncMock()) as post:
        result = await ic.ask(OWNER, "q", "ru", "turn-1")
    assert result == {"outcome": "no_pass"}
    post.assert_not_awaited()


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"outcome": "call_failed"}, True),
        ({"outcome": "tool_timeout"}, True),
        ({"outcome": "source_unavailable"}, True),
        ({"outcome": "http_error", "status": 408}, True),
        ({"outcome": "http_error", "status": 429}, True),
        ({"outcome": "http_error", "status": 500}, True),
        ({"outcome": "http_error", "status": 400}, False),
        ({"outcome": "http_error", "status": 409}, False),
        ({"outcome": "need_login"}, False),
        ({"outcome": "source_unavailable", "fallback_allowed": False}, False),
        ({"outcome": "answer"}, False),
    ],
)
def test_retryable_classes(result, expected):
    assert ic.retryable(result) is expected


def test_retry_after_honours_only_a_short_wait():
    assert ic.retry_delay_s({"outcome": "http_error", "status": 429, "retry_after_s": 2}) == 2
    assert ic.retry_delay_s({"outcome": "http_error", "status": 429, "retry_after_s": 30}) is None
    assert ic.retry_delay_s({"outcome": "call_failed"}) == 0


async def test_ask_never_logs_the_pass_or_the_question(flag_on, caplog):
    async def fake_post(url, token, payload, timeout_s):
        return {"outcome": "answer", "text": "SECRET-ANSWER"}

    caplog.set_level(logging.DEBUG)
    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", fake_post):
        await ic.ask(OWNER, "SECRET-QUESTION", "ru", "turn-1")
    assert PASS_VALUE not in caplog.text
    assert "SECRET-QUESTION" not in caplog.text and "SECRET-ANSWER" not in caplog.text


async def test_state_shows_answer_and_remembers_both_sides_locally(flag_on):
    state = make_state()
    ctx = {}
    with patch.object(ic, "ask", AsyncMock(return_value=ANSWER)), patch(
        "db.queries.activity.record_active_day", AsyncMock()
    ):
        handled, login_hint = await state._answer_via_interlocutor(
            SimpleNamespace(current_context=ctx), OWNER, "вопрос", ctx, "ru", paid_access=True
        )
        await asyncio.sleep(0)
    assert (handled, login_hint) == (True, False)
    shown = " ".join(str(call.args[1]) for call in state.send.await_args_list)
    assert "ответ агента" in shown
    assert ctx["consultation_history"][-1] == {"q": "вопрос", "a": "ответ агента"}
    state._save_session_context.assert_awaited()


@pytest.mark.parametrize("outcome", ["no_pass", "need_login"])
async def test_state_returns_to_the_old_path_with_login_hint_when_login_is_missing(flag_on, outcome):
    state = make_state()
    with patch.object(ic, "ask", AsyncMock(return_value={"outcome": outcome, "text": "войдите"})):
        handled, login_hint = await state._answer_via_interlocutor(
            SimpleNamespace(), OWNER, "вопрос", {}, "ru", paid_access=True
        )
    assert (handled, login_hint) == (False, True)
    state.send.assert_not_awaited()


async def test_temporary_failure_retries_once_with_the_same_turn_id(flag_on):
    state = make_state()
    ask = AsyncMock(side_effect=[{"outcome": "call_failed"}, ANSWER])
    with patch.object(ic, "ask", ask), patch("db.queries.activity.record_active_day", AsyncMock()):
        handled, _ = await state._answer_via_interlocutor(
            SimpleNamespace(), OWNER, "вопрос", {}, "ru", paid_access=True
        )
        await asyncio.sleep(0)
    assert handled is True and ask.await_count == 2
    assert ask.await_args_list[0].args[3] == ask.await_args_list[1].args[3]
    assert [call.kwargs["attempt"] for call in ask.await_args_list] == [1, 2]


async def test_second_temporary_failure_does_not_use_the_old_path_and_keeps_pending(flag_on):
    state = make_state()
    ctx = {}
    with patch.object(ic, "ask", AsyncMock(return_value={"outcome": "call_failed"})) as ask:
        handled, login_hint = await state._answer_via_interlocutor(
            SimpleNamespace(current_context=ctx), OWNER, "вопрос", ctx, "ru", paid_access=True
        )
    assert (handled, login_hint) == (True, False) and ask.await_count == 2
    assert ctx[PENDING_TURN_KEY]["question"] == "вопрос"
    assert ctx[PENDING_TURN_KEY]["expires_at"] > time.time()
    shown = " ".join(str(call.args[1]) for call in state.send.await_args_list)
    assert "временно недоступен" in shown


async def test_nonretryable_http_failure_is_not_retried(flag_on):
    state = make_state()
    with patch.object(ic, "ask", AsyncMock(return_value={"outcome": "http_error", "status": 400})) as ask:
        handled, _ = await state._answer_via_interlocutor(
            SimpleNamespace(), OWNER, "вопрос", {}, "ru", paid_access=True
        )
    assert handled is True and ask.await_count == 1


async def test_forbidden_fallback_is_shown_once_and_clears_pending(flag_on):
    state = make_state()
    ctx = {PENDING_TURN_KEY: {"turn_id": "old-turn", "question": "старый", "expires_at": time.time() + 60}}
    refused = {"outcome": "source_unavailable", "text": "ваш ключ отклонён", "fallback_allowed": False}
    with patch.object(ic, "ask", AsyncMock(return_value=refused)) as ask:
        handled, _ = await state._answer_via_interlocutor(
            SimpleNamespace(current_context=ctx), OWNER, "старый", ctx, "ru", paid_access=True
        )
    assert handled is True and ask.await_count == 1
    assert PENDING_TURN_KEY not in ctx
    assert "ваш ключ отклонён" in " ".join(str(call.args[1]) for call in state.send.await_args_list)


async def test_pending_turn_is_resolved_before_a_different_question(flag_on):
    state = make_state()
    ctx = {PENDING_TURN_KEY: {"turn_id": "old-turn", "question": "старый", "expires_at": time.time() + 60}}
    answers = [{"outcome": "answer", "text": "на старый", "sources": []}, {"outcome": "answer", "text": "на новый", "sources": []}]
    with patch.object(ic, "ask", AsyncMock(side_effect=answers)) as ask, patch(
        "db.queries.activity.record_active_day", AsyncMock()
    ):
        handled, _ = await state._answer_via_interlocutor(
            SimpleNamespace(current_context=ctx), OWNER, "новый", ctx, "ru", paid_access=True
        )
        await asyncio.sleep(0)
    assert handled is True and ask.await_count == 2
    assert ask.await_args_list[0].args[1] == "старый"
    assert ask.await_args_list[0].args[3] == "old-turn"
    assert ask.await_args_list[1].args[1] == "новый"
    assert ask.await_args_list[1].args[3] != "old-turn"
    assert [pair["a"] for pair in ctx["consultation_history"]] == ["на старый", "на новый"]
    assert PENDING_TURN_KEY not in ctx


async def test_expired_pending_is_not_replayed(flag_on):
    state = make_state()
    ctx = {PENDING_TURN_KEY: {"turn_id": "expired-turn", "question": "старый", "expires_at": time.time() - 1}}
    with patch.object(ic, "ask", AsyncMock(return_value=ANSWER)) as ask, patch(
        "db.queries.activity.record_active_day", AsyncMock()
    ):
        await state._answer_via_interlocutor(SimpleNamespace(current_context=ctx), OWNER, "старый", ctx, "ru", paid_access=True)
        await asyncio.sleep(0)
    assert ask.await_args.args[3] != "expired-turn"
    shown = " ".join(str(call.args[1]) for call in state.send.await_args_list)
    assert "Исход прежнего запроса неизвестен" in shown


async def test_delete_pending_failure_blocks_a_new_agent_turn(flag_on):
    state = make_state()
    ctx = {DELETE_PENDING_KEY: True}
    with patch.object(ic, "delete_thread", AsyncMock(return_value={"outcome": "call_failed"})), patch.object(
        ic, "ask", AsyncMock()
    ) as ask:
        handled, _ = await state._answer_via_interlocutor(
            SimpleNamespace(current_context=ctx), OWNER, "новый", ctx, "ru", paid_access=True
        )
    assert handled is True
    ask.assert_not_awaited()
    assert ctx[DELETE_PENDING_KEY] is True


async def test_delete_pending_success_is_cleared_before_the_turn(flag_on):
    state = make_state()
    ctx = {DELETE_PENDING_KEY: True}
    with patch.object(ic, "delete_thread", AsyncMock(return_value={"outcome": "forgotten"})), patch.object(
        ic, "ask", AsyncMock(return_value=ANSWER)
    ), patch("db.queries.activity.record_active_day", AsyncMock()):
        await state._answer_via_interlocutor(SimpleNamespace(current_context=ctx), OWNER, "новый", ctx, "ru", paid_access=True)
        await asyncio.sleep(0)
    assert DELETE_PENDING_KEY not in ctx


async def test_failed_explicit_delete_is_honest_and_keeps_retry_marker(flag_on):
    state = make_state()
    ctx = {DELETE_PENDING_KEY: True}
    with patch.object(ic, "delete_thread", AsyncMock(return_value={"outcome": "call_failed"})):
        deleted = await state._delete_agent_thread(SimpleNamespace(current_context=ctx), OWNER, ctx, "ru")
    assert deleted is False and ctx[DELETE_PENDING_KEY] is True
    shown = " ".join(str(call.args[1]) for call in state.send.await_args_list)
    assert "не подтверждено" in shown


async def test_delete_is_silent_when_the_agent_was_never_used(flag_on):
    state = make_state()
    with patch.object(ic, "delete_thread", AsyncMock()) as delete:
        deleted = await state._delete_agent_thread(SimpleNamespace(), OWNER, {}, "ru")
    assert deleted is True
    delete.assert_not_awaited()
    state.send.assert_not_awaited()


@pytest.mark.parametrize("paid_access, url", [(False, URL), (True, "")])
async def test_state_keeps_the_old_path_for_free_roles_or_when_switched_off(monkeypatch, paid_access, url):
    monkeypatch.setenv("INTERLOCUTOR_URL", url)
    state = make_state()
    with patch.object(ic, "ask", AsyncMock()) as ask:
        handled, login_hint = await state._answer_via_interlocutor(
            SimpleNamespace(), OTHER, "вопрос", {}, "ru", paid_access=paid_access
        )
    assert (handled, login_hint) == (False, False)
    ask.assert_not_awaited()
    state.send.assert_not_awaited()


async def test_plain_text_in_the_main_menu_goes_to_consultation_without_greeting():
    state = ModeSelectState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
    state.enter = AsyncMock()
    dispatcher = SimpleNamespace(is_sm_active=True, go_to=AsyncMock())
    user = {"chat_id": OWNER, "language": "ru"}
    with patch("handlers.get_dispatcher", return_value=dispatcher):
        result = await state.handle(user, SimpleNamespace(text="а какая фаза ближе к завершению"))
    assert result is None
    dispatcher.go_to.assert_awaited_once_with(
        user, "common.consultation", context={"question": "а какая фаза ближе к завершению"}
    )
    state.enter.assert_not_awaited()


def test_session_window_matches_the_agent_thread_ttl():
    assert SESSION_TIMEOUT_SEC == 1800


# ---- свежесть пропуска (агент отвергает истёкший, журнал агента: token_expired)


def _client_with_token(expires_in_s, *, refresh_ok=True, refreshed_to="fresh-pass"):
    from datetime import datetime, timedelta

    from clients.gateway_mcp import GatewayMCPClient

    client = GatewayMCPClient("https://gw.test/mcp")
    expires = None if expires_in_s is None else datetime.utcnow() + timedelta(seconds=expires_in_s)
    client._tokens[OWNER] = {"access_token": "old-pass", "refresh_token": "r", "expires_at": expires, "ory_id": None}

    async def fake_refresh(user_id):
        if refresh_ok and user_id in client._tokens:
            client._tokens[user_id]["access_token"] = refreshed_to
            return True
        return False

    client._refresh_single_token = fake_refresh
    return client


async def test_a_pass_with_time_left_is_returned_as_is():
    assert await _client_with_token(3600).access_token_for(OWNER) == "old-pass"


async def test_an_expired_or_nearly_expired_pass_is_refreshed_before_it_is_returned():
    assert await _client_with_token(-30).access_token_for(OWNER) == "fresh-pass"
    assert await _client_with_token(20).access_token_for(OWNER) == "fresh-pass"


async def test_when_refresh_fails_the_stale_pass_is_not_handed_out():
    assert await _client_with_token(-30, refresh_ok=False).access_token_for(OWNER) is None


async def test_unknown_expiry_is_left_to_the_agent_and_a_missing_person_gives_nothing():
    assert await _client_with_token(None).access_token_for(OWNER) == "old-pass"
    assert await _client_with_token(3600).access_token_for(OTHER) is None


async def test_timeout_deletes_the_agent_thread_and_keeps_pending_on_failure(flag_on):
    state = make_state()
    ctx = {
        "interlocutor_agent_used": True,
        "consultation_history": [{"q": "q", "a": "a"}],
        "consultation_last_activity": time.time() - 31 * 60,
    }
    user = SimpleNamespace(current_context=ctx, language="ru")
    with patch.object(ic, "delete_thread", AsyncMock(return_value={"outcome": "call_failed"})) as delete:
        await state._expire_session(user, ctx, OWNER)
    delete.assert_awaited_once_with(OWNER)
    assert ctx[DELETE_PENDING_KEY] is True
    assert "consultation_history" not in ctx


async def test_retry_after_delay_blocks_an_early_manual_retry(flag_on):
    state = make_state()
    result = {"outcome": "http_error", "status": 429, "retry_after_s": 30}
    ctx = {PENDING_TURN_KEY: {
        "turn_id": "limited-turn",
        "question": "вопрос",
        "expires_at": time.time() + 60,
        "not_before": time.time() + 30,
        "last_result": result,
    }}
    user = SimpleNamespace(current_context=ctx)
    with patch.object(ic, "ask", AsyncMock()) as ask:
        handled, _ = await state._answer_via_interlocutor(user, OWNER, "вопрос", ctx, "ru", paid_access=True)
    assert handled is True
    ask.assert_not_awaited()
    assert ctx[PENDING_TURN_KEY]["turn_id"] == "limited-turn"


async def test_slash_command_in_the_main_menu_does_not_go_to_the_agent():
    state = ModeSelectState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
    state.enter = AsyncMock()
    dispatcher = SimpleNamespace(is_sm_active=True, go_to=AsyncMock())
    with patch("handlers.get_dispatcher", return_value=dispatcher):
        await state.handle({"chat_id": OWNER, "language": "ru"}, SimpleNamespace(text="/help"))
    dispatcher.go_to.assert_not_awaited()
    state.enter.assert_awaited_once()


async def test_stale_epoch_does_not_recreate_a_deleted_thread(flag_on):
    state = make_state()
    old_ctx = {"interlocutor_session_epoch": "old"}
    user = SimpleNamespace(current_context={"interlocutor_session_epoch": "new"})
    with patch.object(ic, "ask", AsyncMock()) as ask:
        handled, _ = await state._answer_via_interlocutor(user, OWNER, "старый вопрос", old_ctx, "ru", paid_access=True)
    assert handled is True
    ask.assert_not_awaited()
    assert "interlocutor_agent_used" not in old_ctx


async def test_timeout_does_not_delete_a_session_updated_while_waiting(flag_on):
    state = make_state()
    ctx = {
        "interlocutor_agent_used": True,
        "consultation_history": [{"q": "q", "a": "a"}],
        "consultation_last_activity": time.time(),
    }
    user = SimpleNamespace(current_context=ctx, language="ru")
    with patch.object(ic, "delete_thread", AsyncMock()) as delete:
        deleted = await state._delete_agent_thread(user, OWNER, ctx, "ru", clear_reason="timeout")
    assert deleted is True
    delete.assert_not_awaited()
    assert ctx["consultation_history"] == [{"q": "q", "a": "a"}]


async def test_disabled_agent_keeps_delete_pending_for_reenable(flag_on, monkeypatch):
    state = make_state()
    ctx = {"interlocutor_agent_used": True}
    user = SimpleNamespace(current_context=ctx, language="ru")
    monkeypatch.delenv("INTERLOCUTOR_URL", raising=False)
    deleted = await state._delete_agent_thread(user, OWNER, ctx, "ru", clear_reason="exit")
    assert deleted is True
    assert ctx[DELETE_PENDING_KEY] is True


async def test_paid_temporary_failure_never_reaches_the_legacy_model(flag_on):
    state = make_state()
    ctx = {}
    user = {"chat_id": OWNER, "current_context": ctx, "language": "ru"}
    state._save_session_context = AsyncMock(side_effect=lambda _chat_id, new_ctx: user.__setitem__("current_context", new_ctx))
    legacy = AsyncMock()
    with patch("core.access.access_layer.has_access", AsyncMock(return_value=True)), patch.object(
        ic, "ask", AsyncMock(return_value={"outcome": "call_failed"})
    ) as ask, patch("engines.shared.handle_question_with_tools", legacy):
        await state.enter(user, context={"question": "временный сбой"})
    assert ask.await_count == 2
    legacy.assert_not_awaited()
    assert user["current_context"][PENDING_TURN_KEY]["question"] == "временный сбой"


@pytest.mark.parametrize("outcome", ["no_pass", "need_login"])
async def test_missing_login_uses_legacy_answer_and_shows_login_button(flag_on, outcome):
    state = make_state()
    ctx = {}
    user = {"chat_id": OWNER, "current_context": ctx, "language": "ru"}
    state._save_session_context = AsyncMock(side_effect=lambda _chat_id, new_ctx: user.__setitem__("current_context", new_ctx))
    ory = SimpleNamespace(get_authorization_url=AsyncMock(return_value=("https://login.test", "state")))
    with patch("core.access.access_layer.has_access", AsyncMock(return_value=True)), patch.object(
        ic, "ask", AsyncMock(return_value={"outcome": outcome})
    ), patch("states.common.consultation.save_qa", AsyncMock(return_value=None)), patch(
        "clients.ory_oauth.ory_oauth", ory
    ):
        await state.enter(user, context={"question": "что ты умеешь"})
    shown = " ".join(str(call.args[1]) for call in state.send.await_args_list)
    assert "Что я умею" in shown
    assert "войдите в аккаунт МИМ" in shown
    assert user["current_context"]["consultation_history"][-1]["q"] == "что ты умеешь"


@pytest.mark.parametrize("exit_kind", ["exit", "end", "role_entry"])
@pytest.mark.parametrize("thread_key", ["interlocutor_agent_used", PENDING_TURN_KEY, DELETE_PENDING_KEY])
async def test_every_exit_deletes_every_kind_of_pending_thread(flag_on, exit_kind, thread_key):
    state = make_state()
    pending_value = {"turn_id": "t", "question": "q", "expires_at": time.time() + 60} if thread_key == PENDING_TURN_KEY else True
    ctx = {thread_key: pending_value, "consultation_history": [{"q": "q", "a": "a"}]}
    user = {"chat_id": OWNER, "current_context": ctx, "language": "ru"}
    with patch.object(ic, "delete_thread", AsyncMock(return_value={"outcome": "forgotten"})) as delete:
        if exit_kind == "exit":
            await state.exit(user)
        elif exit_kind == "end":
            await state._end_session(user, ctx, "ru")
        else:
            await state.enter(user, context={"force_role": "navigator"})
    delete.assert_awaited_once_with(OWNER)
    assert DELETE_PENDING_KEY not in ctx
