"""
РП-578 Ф21.4: /diagnose записывает самооценку только через инструмент шлюза.

Проверяет: прямой writer не вызывается; ключ попытки стабилен при повторах; FSM чистится
только после полного успеха; идентичность токена сверяется до записи; исходы
(сохранено / без события / нет авторизации / исход неизвестен) различаются в тексте.
"""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("KNOWLEDGE_MCP_URL", "https://fake-mcp.test/mcp")
os.environ.setdefault("USE_STATE_MACHINE", "false")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from handlers import diagnose
from handlers.diagnose import (
    SAVE_AUTH,
    SAVE_IDENTITY,
    SAVE_NO_EVENT,
    SAVE_OK,
    SAVE_UNKNOWN,
    classify_save_result,
)

ACCOUNT = "11111111-1111-1111-1111-111111111111"
CHAT_ID = 777


class FakeState:
    """Минимальный FSMContext: данные и состояние в памяти."""

    def __init__(self, data):
        self.data = dict(data)
        self.fsm_state = "q6"

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, value=None):
        self.fsm_state = value

    async def clear(self):
        self.data = {}
        self.fsm_state = None


def make_message():
    message = MagicMock()
    message.chat.id = CHAT_ID
    message.answer = AsyncMock()
    message.bot = MagicMock()
    return message


def finished_data(attempt_id="attempt-1"):
    return {
        "scores": {"cp.rhy": 3, "cp.wld": 3, "cp.skl": 3, "cp.int": 4, "cp.agt": 3, "cp.iwe": 2},
        "account_id": ACCOUNT,
        "q_count": 6,
        "attempt_id": attempt_id,
    }


def gateway(token_ory_id=ACCOUNT, connected=True, result=None, last_error="ok"):
    gw = MagicMock()
    gw.token_ory_id = AsyncMock(return_value=token_ory_id)
    gw.is_connected = MagicMock(return_value=connected)
    gw.record_cp_assessment = AsyncMock(return_value=result)
    gw.last_call_error = last_error
    return gw


def ok_result(event_recorded=True, duplicate=False):
    return {"status": "pending_evaluation", "event_id": "x", "duplicate": duplicate, "event_recorded": event_recorded}


LOGIN_URL = "https://auth.example/login?state=x"


async def run_finish(state, gw, login_url=LOGIN_URL):
    message = make_message()
    with patch("clients.gateway_mcp.gateway_mcp", gw), \
         patch("handlers.diagnose._login_url", AsyncMock(return_value=login_url)), \
         patch("core.onboarder.x3.check_x3_return_to_bridge", AsyncMock()) as bridge, \
         patch("db.queries.onboarding_journey.sync_cp_stage_to_onboarding_state", AsyncMock()) as sync:
        await diagnose._finish_diagnose(message, state)
    return message, bridge, sync


async def run_retry(state, gw, login_url=LOGIN_URL):
    message = make_message()
    with patch("clients.gateway_mcp.gateway_mcp", gw), \
         patch("handlers.diagnose._login_url", AsyncMock(return_value=login_url)), \
         patch("db.queries.onboarding_journey.sync_cp_stage_to_onboarding_state", AsyncMock()) as sync:
        await diagnose._save_attempt(message, state, first_pass=False)
    return message, sync


def texts(message):
    return [call.args[0] for call in message.answer.await_args_list]


def last_buttons(message):
    """(text, url, callback_data) of every button on the last answer, row by row."""
    markup = message.answer.await_args_list[-1].kwargs.get("reply_markup")
    if markup is None:
        return []
    return [(b.text, b.url, b.callback_data) for row in markup.inline_keyboard for b in row]


class TestClassify:
    def test_confirmed_with_event(self):
        assert classify_save_result(ok_result(True), "ok") == SAVE_OK

    def test_confirmed_duplicate_is_still_saved(self):
        assert classify_save_result(ok_result(True, duplicate=True), "ok") == SAVE_OK

    def test_saved_without_event(self):
        assert classify_save_result(ok_result(False), "ok") == SAVE_NO_EVENT

    @pytest.mark.parametrize("error", ["not_authorized", "token_expired"])
    def test_auth_errors(self, error):
        assert classify_save_result(None, error) == SAVE_AUTH

    @pytest.mark.parametrize("error", ["timeout", "http_error", "circuit_open", "rpc_error", "rate_limited", "ok",
                                       "no_subscription"])
    def test_other_failures_are_unknown_not_auth(self, error):
        assert classify_save_result(None, error) == SAVE_UNKNOWN

    def test_unexpected_shape_is_unknown(self):
        assert classify_save_result("text", "ok") == SAVE_UNKNOWN


class TestFinishDiagnose:
    async def test_success_writes_through_tool_and_clears_fsm(self):
        state = FakeState(finished_data())
        gw = gateway(result=ok_result())
        message, bridge, sync = await run_finish(state, gw)

        call = gw.record_cp_assessment.await_args
        assert call.args == (CHAT_ID,)
        assert call.kwargs["event_id"] == "tg-diagnose:attempt-1"
        assert call.kwargs["profile"]["stage"] == 3
        assert call.kwargs["cp_scores"]["cp.skl"] == 3  # own answer (WP-385 Ф18.7)
        assert call.kwargs["questions_count"] == 6
        assert state.data == {}  # очищено после полного успеха
        sync.assert_awaited_once_with(ACCOUNT, 3)
        bridge.assert_awaited_once()
        shown = texts(message)
        assert len(shown) == 1 and "Результаты диагностики" in shown[0] and "Ступень" in shown[0]

    async def test_direct_writer_is_not_imported_by_handler(self):
        assert not hasattr(diagnose, "save_cp_assessment")

    async def test_unknown_outcome_keeps_attempt_and_offers_retry(self):
        state = FakeState(finished_data())
        gw = gateway(result=None, last_error="timeout")
        message, _bridge, sync = await run_finish(state, gw)

        pending = state.data["pending_save"]
        assert pending["event_id"] == "tg-diagnose:attempt-1"
        assert pending["profile"]["stage"] == 3
        assert state.fsm_state is None
        sync.assert_not_awaited()  # запись не подтверждена
        last = message.answer.await_args_list[-1]
        assert "Повторить сохранение" in last.kwargs["reply_markup"].inline_keyboard[0][0].text
        assert last.args[0] == diagnose._SAVE_NOTES[SAVE_UNKNOWN]

    async def test_retry_reuses_same_key_and_time(self):
        state = FakeState(finished_data())
        await run_finish(state, gateway(result=None, last_error="timeout"))
        first = dict(state.data["pending_save"])

        gw = gateway(result=ok_result(duplicate=True))
        message, sync = await run_retry(state, gw)

        kwargs = gw.record_cp_assessment.await_args.kwargs
        assert kwargs["event_id"] == first["event_id"]
        assert kwargs["occurred_at"] == first["occurred_at"]
        assert kwargs["cp_scores"] == first["cp_scores"]
        assert state.data["pending_save"] is None
        assert texts(message) == ["✅ Результат сохранён."]
        sync.assert_awaited_once()

    async def test_saved_without_event_keeps_attempt_for_retry(self):
        state = FakeState(finished_data())
        gw = gateway(result=ok_result(event_recorded=False))
        message, _bridge, sync = await run_finish(state, gw)

        assert "pending_save" in state.data
        sync.assert_awaited_once()  # самооценка записана
        assert texts(message)[-1] == diagnose._SAVE_NOTES[SAVE_NO_EVENT]

    async def test_new_attempt_gets_new_key(self):
        first, second = FakeState(finished_data("a")), FakeState(finished_data("b"))
        gw = gateway(result=ok_result())
        await run_finish(first, gw)
        await run_finish(second, gw)
        keys = [call.kwargs["event_id"] for call in gw.record_cp_assessment.await_args_list]
        assert keys == ["tg-diagnose:a", "tg-diagnose:b"]

    async def test_missing_attempt_id_still_gets_stable_key_for_retries(self):
        data = finished_data()
        data.pop("attempt_id")
        state = FakeState(data)
        await run_finish(state, gateway(result=None, last_error="timeout"))
        assert state.data["pending_save"]["event_id"].startswith("tg-diagnose:")
        assert len(state.data["pending_save"]["event_id"]) > len("tg-diagnose:")

    async def test_identity_mismatch_does_not_write(self):
        state = FakeState(finished_data())
        gw = gateway(token_ory_id="22222222-2222-2222-2222-222222222222")
        message, _bridge, sync = await run_finish(state, gw)

        gw.record_cp_assessment.assert_not_awaited()
        sync.assert_not_awaited()
        assert texts(message)[-1] == diagnose._save_note(SAVE_IDENTITY, has_link=True)
        assert "pending_save" in state.data

    async def test_token_without_ory_id_cannot_be_verified_and_does_not_write(self):
        gw = gateway(token_ory_id=None, connected=True)
        message, _bridge, _sync = await run_finish(FakeState(finished_data()), gw)
        gw.record_cp_assessment.assert_not_awaited()
        assert texts(message)[-1] == diagnose._save_note(SAVE_IDENTITY, has_link=True)

    async def test_not_connected_user_gets_auth_message(self):
        gw = gateway(token_ory_id=None, connected=False)
        message, _bridge, _sync = await run_finish(FakeState(finished_data()), gw)
        gw.record_cp_assessment.assert_not_awaited()
        assert texts(message)[-1] == diagnose._save_note(SAVE_AUTH, has_link=True)

    async def test_identity_comparison_ignores_case(self):
        state = FakeState(finished_data())
        gw = gateway(token_ory_id=ACCOUNT.upper(), result=ok_result())
        await run_finish(state, gw)
        gw.record_cp_assessment.assert_awaited_once()

    async def test_no_account_shows_result_without_save(self):
        data = finished_data()
        data["account_id"] = None
        state = FakeState(data)
        gw = gateway(result=ok_result())
        message, bridge, _sync = await run_finish(state, gw)
        gw.record_cp_assessment.assert_not_awaited()
        bridge.assert_awaited_once()
        assert state.data == {}

    async def test_run_without_skill_answer_is_not_saved(self):
        """A run from the old question set (no cp.skl answer) must not be saved with a guess."""
        data = finished_data()
        del data["scores"]["cp.skl"]
        state = FakeState(data)
        gw = gateway(result=ok_result())
        message, _bridge, _sync = await run_finish(state, gw)
        gw.record_cp_assessment.assert_not_awaited()
        assert "Получено ответов: 4 из 5" in texts(message)[0]

    async def test_partial_run_is_not_saved(self):
        data = finished_data()
        data["q_count"] = 2
        state = FakeState(data)
        gw = gateway(result=ok_result())
        message, _bridge, _sync = await run_finish(state, gw)
        gw.record_cp_assessment.assert_not_awaited()
        assert "Диагностика не завершена" in texts(message)[0]


class TestLoginNote:
    """WP-578 F29 p.8 (b): an unsaved result says so and tells how to connect the account."""

    async def test_not_connected_gets_login_button_steps_and_retry(self):
        gw = gateway(token_ory_id=None, connected=False)
        message, _bridge, _sync = await run_finish(FakeState(finished_data()), gw)

        shown = texts(message)
        assert "пока не сохранены" in shown[0]
        assert "Как подключить аккаунт" in shown[-1]
        assert "Регистрация завершена!" in shown[-1]
        assert last_buttons(message) == [
            (diagnose.CONNECT_BUTTON, LOGIN_URL, None),
            (diagnose.RETRY_BUTTON, None, diagnose.CB_RETRY_SAVE),
        ]

    async def test_identity_mismatch_gets_login_button_for_reconnect(self):
        gw = gateway(token_ory_id="22222222-2222-2222-2222-222222222222")
        message, _bridge, _sync = await run_finish(FakeState(finished_data()), gw)
        assert texts(message)[-1] == diagnose._save_note(SAVE_IDENTITY, has_link=True)
        assert "Новый аккаунт не создавайте" in texts(message)[-1]
        assert last_buttons(message)[0] == (diagnose.CONNECT_BUTTON, LOGIN_URL, None)

    async def test_no_account_says_not_saved_with_login_and_no_retry(self):
        data = finished_data()
        data["account_id"] = None
        message, _bridge, _sync = await run_finish(FakeState(data), gateway(result=ok_result()))

        shown = texts(message)
        assert "пока не сохранены" in shown[0]
        assert shown[-1] == diagnose._save_note(diagnose.SAVE_NO_ACCOUNT, has_link=True)
        assert "там же можно зарегистрироваться" in shown[-1]
        # Nothing is pending, so no retry; a second button builds a fresh link after 10 minutes.
        assert last_buttons(message) == [
            (diagnose.CONNECT_BUTTON, LOGIN_URL, None),
            (diagnose.REGISTER_BUTTON, None, "ory_register"),
        ]

    async def test_missing_login_link_is_said_in_text_and_retry_stays(self):
        gw = gateway(token_ory_id=None, connected=False)
        message, _bridge, _sync = await run_finish(FakeState(finished_data()), gw, login_url=None)
        note = texts(message)[-1]
        assert note == diagnose._save_note(SAVE_AUTH, has_link=False)
        assert "Кнопка входа сейчас недоступна" in note and "под этим сообщением" not in note
        assert "Повторить сохранение» позже" in note
        assert last_buttons(message) == [(diagnose.RETRY_BUTTON, None, diagnose.CB_RETRY_SAVE)]

    async def test_no_account_without_link_has_no_keyboard(self):
        data = finished_data()
        data["account_id"] = None
        message, _bridge, _sync = await run_finish(FakeState(data), gateway(), login_url=None)
        assert texts(message)[-1] == diagnose._save_note(diagnose.SAVE_NO_ACCOUNT, has_link=False)
        assert message.answer.await_args_list[-1].kwargs.get("reply_markup") is None

    async def test_known_account_is_told_not_to_sign_up(self):
        """A new sign-up would relink the bot profile to an empty account (review 23.11)."""
        for outcome in (SAVE_AUTH, SAVE_IDENTITY):
            note = diagnose._save_note(outcome, has_link=True)
            assert "Новый аккаунт не создавайте" in note
            assert "зарегистрироваться" not in note
            assert "Ссылка входа действует 10 минут" in note

    async def test_retry_while_still_not_connected_sends_fresh_login_and_retry(self):
        state = FakeState(finished_data())
        gw = gateway(token_ory_id=None, connected=False)
        await run_finish(state, gw)
        message, _sync = await run_retry(state, gw, login_url="https://auth.example/fresh")
        assert texts(message) == [diagnose._save_note(SAVE_AUTH, has_link=True)]
        assert last_buttons(message) == [
            (diagnose.CONNECT_BUTTON, "https://auth.example/fresh", None),
            (diagnose.RETRY_BUTTON, None, diagnose.CB_RETRY_SAVE),
        ]
        assert "pending_save" in state.data

    async def test_retry_after_login_saves_the_same_attempt(self):
        state = FakeState(finished_data())
        await run_finish(state, gateway(token_ory_id=None, connected=False))
        gw = gateway(token_ory_id=ACCOUNT, result=ok_result())
        message, _sync = await run_retry(state, gw)
        gw.record_cp_assessment.assert_awaited_once()
        assert texts(message) == ["✅ Результат сохранён."]

    @pytest.mark.parametrize("gw_kwargs", [
        {"token_ory_id": ACCOUNT, "result": ok_result(event_recorded=False)},
        {"token_ory_id": ACCOUNT, "result": None, "last_error": "timeout"},
    ])
    async def test_non_login_outcomes_do_not_say_unsaved_in_title(self, gw_kwargs):
        message, _bridge, _sync = await run_finish(FakeState(finished_data()), gateway(**gw_kwargs))
        assert "Результаты диагностики" in texts(message)[0]
        assert "пока не сохранены" not in texts(message)[0]

    async def test_saved_result_title_does_not_say_unsaved(self):
        gw = gateway(token_ory_id=ACCOUNT, result=ok_result())
        message, _bridge, _sync = await run_finish(FakeState(finished_data()), gw)
        assert "Результаты диагностики" in texts(message)[0]
        assert "пока не сохранены" not in texts(message)[0]

    async def test_unknown_outcome_has_no_login_button(self):
        gw = gateway(result=None, last_error="timeout")
        message, _bridge, _sync = await run_finish(FakeState(finished_data()), gw)
        assert last_buttons(message) == [(diagnose.RETRY_BUTTON, None, diagnose.CB_RETRY_SAVE)]


class TestLoginUrl:
    async def test_empty_client_id_gives_no_link(self):
        with patch("config.ORY_CLIENT_ID", ""):
            assert await diagnose._login_url(CHAT_ID) is None

    async def test_link_error_is_logged_and_gives_no_link(self):
        oauth = MagicMock()
        oauth.get_authorization_url = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("config.ORY_CLIENT_ID", "client"), patch("clients.ory_oauth.ory_oauth", oauth), \
             patch.object(diagnose.logger, "exception") as log:
            assert await diagnose._login_url(CHAT_ID) is None
        log.assert_called_once()

    async def test_link_is_built_for_this_chat(self):
        oauth = MagicMock()
        oauth.get_authorization_url = AsyncMock(return_value=(LOGIN_URL, "state"))
        with patch("config.ORY_CLIENT_ID", "client"), patch("clients.ory_oauth.ory_oauth", oauth):
            assert await diagnose._login_url(CHAT_ID) == LOGIN_URL
        oauth.get_authorization_url.assert_awaited_once_with(CHAT_ID)


class TestFailureSafety:
    async def test_exception_in_gateway_still_shows_result_and_retry(self):
        state = FakeState(finished_data())
        gw = gateway(result=None)
        gw.record_cp_assessment = AsyncMock(side_effect=RuntimeError("db down"))
        message, bridge, sync = await run_finish(state, gw)

        shown = texts(message)
        assert "Результаты диагностики" in shown[0]
        assert shown[-1] == diagnose._SAVE_NOTES[SAVE_UNKNOWN]
        assert "pending_save" in state.data
        sync.assert_not_awaited()
        bridge.assert_awaited_once()

    async def test_successful_retry_keeps_unrelated_fsm_data(self):
        state = FakeState({"pending_save": {
            "event_id": "tg-diagnose:a", "occurred_at": "2026-10-29T10:00:00+00:00", "account_id": ACCOUNT,
            "profile": {"stage": 3, "bottleneck_slot": "cp.rhy", "recommended_stream": "S3", "skip_to_stage": 3},
            "cp_scores": {"cp.rhy": 3}, "questions_count": 5,
        }, "other_scenario": "kept"})
        await run_retry(state, gateway(result=ok_result()))
        assert state.data == {"pending_save": None, "other_scenario": "kept"}


class TestRetryWithoutAttempt:
    async def test_retry_with_empty_fsm_says_nothing_to_save(self):
        gw = gateway(result=ok_result())
        message, _sync = await run_retry(FakeState({}), gw)
        gw.record_cp_assessment.assert_not_awaited()
        assert "Нет несохранённой диагностики" in texts(message)[0]


class TestStartPhase:
    async def test_start_creates_attempt_id_and_clears_pending(self):
        state = FakeState({"pending_save": {"old": 1}})
        message = make_message()
        with patch.object(diagnose, "send_diagnostic_question", AsyncMock()):
            await diagnose._start_phase1(message, state, ACCOUNT)
        assert state.data["pending_save"] is None
        assert len(state.data["attempt_id"]) == 36
        first = state.data["attempt_id"]
        with patch.object(diagnose, "send_diagnostic_question", AsyncMock()):
            await diagnose._start_phase1(message, state, ACCOUNT)
        assert state.data["attempt_id"] != first


class TestRecordTimeout:
    async def test_record_call_uses_long_timeout(self):
        from clients.gateway_mcp import GatewayMCPClient

        client = GatewayMCPClient("https://gateway.test/mcp")
        client._call = AsyncMock(return_value={"content": [{"type": "text", "text": '{"event_recorded": true}'}]})
        profile = {"stage": 3, "bottleneck_slot": "cp.rhy", "recommended_stream": "S3", "skip_to_stage": 3}
        result = await client.record_cp_assessment(
            CHAT_ID, event_id="tg-diagnose:a", profile=profile, cp_scores={"cp.rhy": 3},
            questions_count=5, occurred_at="2026-10-29T10:00:00+00:00",
        )
        assert result == {"event_recorded": True}
        call = client._call.await_args
        assert call.args[0] == "learning_ctx_record_cp_assessment"
        assert call.kwargs["timeout"] == 15
        assert call.args[1]["bottleneck_slot"] == "cp.rhy" and call.args[1]["written_by"] == "tg"


REFORMER = {"level": "Реформатор", "numeric": 70, "source": "lms"}


class TestQualificationInsteadOfStage:
    """WP-417 Ф8: from «Работник» up the result names the qualification, not the stage."""

    async def test_reformer_result_shows_qualification_read_before_fsm_changes(self):
        state = FakeState(finished_data())
        gw = gateway(result=ok_result())
        fsm_at_read = []

        async def read_degree(chat_id):
            fsm_at_read.append(state.fsm_state)
            return REFORMER

        gw.dt_read_degree = AsyncMock(side_effect=read_degree)
        message, _, _ = await run_finish(state, gw)

        shown = texts(message)
        assert "🎓 Квалификация: <b>Реформатор</b>" in shown[0] and "Ступень" not in shown[0]
        assert fsm_at_read == ["q6"]          # twin read before set_state(None)
        assert state.data == {}

    async def test_failed_twin_read_keeps_the_stage(self):
        state = FakeState(finished_data())
        gw = gateway(result=ok_result())
        gw.dt_read_degree = AsyncMock(side_effect=TimeoutError("down"))
        message, _, _ = await run_finish(state, gw)

        assert "Ступень" in texts(message)[0]

    async def test_fresh_diagnostic_reply_shows_qualification(self):
        from contextlib import asynccontextmanager
        from datetime import datetime, timezone

        @asynccontextmanager
        async def no_typing(_message):
            yield

        existing = {"stage": 5, "recommended_stream": "РР",
                    "assessed_at": datetime.now(timezone.utc).isoformat()}
        gw = gateway()
        gw.dt_read_degree = AsyncMock(return_value=REFORMER)
        message = make_message()
        with patch("clients.gateway_mcp.gateway_mcp", gw), \
             patch("helpers.dual_write.resolve_ory_id_from_chat", AsyncMock(return_value=ACCOUNT)), \
             patch.object(diagnose, "get_latest_cp_assessment", AsyncMock(return_value=existing)), \
             patch.object(diagnose, "keep_typing", no_typing):
            await diagnose.cmd_diagnose(message, FakeState({}))

        shown = texts(message)
        assert len(shown) == 1 and "свежая диагностика" in shown[0]
        assert "🎓 Квалификация: <b>Реформатор</b>" in shown[0] and "Ступень" not in shown[0]
