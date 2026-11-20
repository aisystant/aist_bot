"""РП-262 Ф4: клиент агента платформы и его встраивание в свободный чат.

Покрывает engines.shared.interlocutor_client (флаг, история, вызов, запасной путь) и
states.common.consultation._answer_via_interlocutor (показ ответа, история, возврат к прежнему пути).
"""

import asyncio
import logging
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
from states.common.consultation import ConsultationState  # noqa: E402

OWNER = 424242
OTHER = 777
URL = "https://agent.test/v1/turn"
PASS_VALUE = "person-pass-that-must-never-be-logged"


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("INTERLOCUTOR_URL", URL)


def test_flag_is_on_only_with_a_url(monkeypatch):
    monkeypatch.delenv("INTERLOCUTOR_URL", raising=False)
    assert ic.is_enabled() is False
    monkeypatch.setenv("INTERLOCUTOR_URL", "   ")
    assert ic.is_enabled() is False
    monkeypatch.setenv("INTERLOCUTOR_URL", URL)
    assert ic.is_enabled() is True


def test_history_keeps_the_last_five_pairs_in_contract_form():
    pairs = [{"q": f"q{i}", "a": f"a{i}"} for i in range(8)] + [{"q": "", "a": "broken"}]
    items = ic.build_history(pairs)
    assert len(items) == 8  # last 5 pairs, of which the empty one is dropped -> 4 pairs
    assert items[0] == {"role": "user", "text": "q4"}
    assert items[1] == {"role": "assistant", "text": "a4"}
    assert items[-1] == {"role": "assistant", "text": "a7"}
    assert ic.build_history(None) == []
    assert len(ic.build_history([{"q": "x" * 9000, "a": "y"}])[0]["text"]) == ic.MAX_TEXT_CHARS


def test_source_labels_prefer_reference_dedupe_and_cap():
    sources = [
        {"tool": "personal_search", "ref": "notes/a.md"},
        {"tool": "personal_search", "ref": "notes/a.md"},
        {"tool": "whoami", "ref": None},
        {"tool": "x", "ref": "third"},
        "junk",
    ]
    assert ic.source_labels(sources) == ["notes/a.md", "whoami"]
    assert ic.source_labels(None) == []


def _patch_token(token):
    gateway = MagicMock()
    gateway.access_token_for = AsyncMock(return_value=token)
    return patch("clients.gateway_mcp.gateway_mcp", gateway)


async def test_ask_sends_the_persons_pass_and_returns_an_answer(flag_on):
    seen = {}

    async def fake_post(url, token, payload, timeout_s):
        seen.update(url=url, token=token, payload=payload)
        return {"outcome": "answer", "text": "готово", "sources": [{"tool": "t", "ref": "r"}]}

    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", fake_post):
        result = await ic.ask(OWNER, "вопрос", [{"q": "раньше", "a": "ответ"}], "ru")
    assert result["text"] == "готово"
    assert seen["url"] == URL and seen["token"] == PASS_VALUE
    assert seen["payload"]["text"] == "вопрос"
    assert seen["payload"]["language"] == "ru"
    assert [h["role"] for h in seen["payload"]["history"]] == ["user", "assistant"]
    assert seen["payload"]["turn_id"].startswith(f"{OWNER}:")
    assert PASS_VALUE not in str(seen["payload"])  # the pass travels in the header only


@pytest.mark.parametrize(
    "reply",
    [
        {"outcome": "need_login", "text": "войдите"},
        {"outcome": "tool_timeout", "text": "не успел"},
        {"outcome": "source_unavailable", "text": "нет доступа"},
        {"outcome": "source_unavailable", "text": "нет доступа", "fallback_allowed": True},
        {"outcome": "source_unavailable", "text": "  ", "fallback_allowed": False},
        {"outcome": "answer", "text": "   "},
        {"outcome": "answer"},
        None,
    ],
)
async def test_ask_returns_nothing_unless_the_agent_really_answered(flag_on, reply):
    async def fake_post(url, token, payload, timeout_s):
        return reply

    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", fake_post):
        assert await ic.ask(OWNER, "вопрос", None, "ru") is None


async def test_ask_returns_a_message_the_agent_forbids_to_replace(flag_on):
    refused = {"outcome": "source_unavailable", "text": "ключ отклонён", "fallback_allowed": False}

    async def fake_post(url, token, payload, timeout_s):
        return refused

    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", fake_post):
        assert await ic.ask(OWNER, "вопрос", None, "ru") == refused


async def test_ask_survives_network_failure_and_missing_pass(flag_on):
    async def boom(url, token, payload, timeout_s):
        raise aiohttp.ClientConnectionError("down")

    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", boom):
        assert await ic.ask(OWNER, "q", None, "ru") is None
    with _patch_token(None), patch.object(ic, "_post_json", AsyncMock()) as post:
        assert await ic.ask(OWNER, "q", None, "ru") is None
        post.assert_not_awaited()  # no pass: the agent is not even called


async def test_ask_never_logs_the_pass_or_the_question(flag_on, caplog):
    async def fake_post(url, token, payload, timeout_s):
        return {"outcome": "answer", "text": "SECRET-ANSWER"}

    caplog.set_level(logging.DEBUG)
    with _patch_token(PASS_VALUE), patch.object(ic, "_post_json", fake_post):
        await ic.ask(OWNER, "SECRET-QUESTION", None, "ru")
    assert PASS_VALUE not in caplog.text
    assert "SECRET-QUESTION" not in caplog.text and "SECRET-ANSWER" not in caplog.text


# ---- встраивание в состояние консультации


def make_state():
    state = ConsultationState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
    state.send = AsyncMock()
    state._keep_typing = MagicMock(return_value=MagicMock(cancel=MagicMock()))
    state._save_session_context = AsyncMock()
    state._get_mode = MagicMock(return_value="marathon")
    return state


async def test_state_shows_the_agent_answer_and_remembers_the_pair(flag_on):
    state = make_state()
    ctx = {}
    reply = {"outcome": "answer", "text": "ответ агента", "sources": [{"tool": "personal_search", "ref": "a.md"}]}
    with patch.object(ic, "ask", AsyncMock(return_value=reply)), patch(
        "db.queries.activity.record_active_day", AsyncMock()
    ):
        handled = await state._answer_via_interlocutor(SimpleNamespace(), OWNER, "вопрос", ctx, "ru", paid_access=True)
        await asyncio.sleep(0)
    assert handled is True
    shown = " ".join(str(call.args[1]) for call in state.send.await_args_list)
    assert "ответ агента" in shown and "a.md" in shown
    assert ctx["consultation_history"][-1]["q"] == "вопрос"
    assert "ответ агента" in ctx["consultation_history"][-1]["a"]
    state._save_session_context.assert_awaited()


async def test_state_shows_a_refused_key_message_and_does_not_fall_back_or_remember_it(flag_on):
    state = make_state()
    ctx = {}
    refused = {"outcome": "source_unavailable", "text": "ваш ключ отклонён", "fallback_allowed": False}
    with patch.object(ic, "ask", AsyncMock(return_value=refused)):
        handled = await state._answer_via_interlocutor(SimpleNamespace(), OWNER, "вопрос", ctx, "ru", paid_access=True)
    assert handled is True  # the old path, paid by the platform, does not run
    shown = " ".join(str(call.args[1]) for call in state.send.await_args_list)
    assert "ваш ключ отклонён" in shown
    assert "consultation_history" not in ctx
    state._save_session_context.assert_not_awaited()


async def test_state_falls_back_when_the_agent_gives_nothing(flag_on):
    state = make_state()
    ctx = {}
    with patch.object(ic, "ask", AsyncMock(return_value=None)):
        handled = await state._answer_via_interlocutor(SimpleNamespace(), OWNER, "вопрос", ctx, "ru", paid_access=True)
    assert handled is False  # the old path continues
    assert "consultation_history" not in ctx
    state._save_session_context.assert_not_awaited()


async def test_state_calls_the_agent_for_any_subscriber_not_a_listed_few(flag_on, monkeypatch):
    monkeypatch.setenv("INTERLOCUTOR_TG_IDS", str(OWNER))  # the old per-person list no longer matters
    state = make_state()
    reply = {"outcome": "answer", "text": "ответ агента", "sources": []}
    ask = AsyncMock(return_value=reply)
    with patch.object(ic, "ask", ask), patch("db.queries.activity.record_active_day", AsyncMock()):
        handled = await state._answer_via_interlocutor(
            SimpleNamespace(), OTHER, "вопрос", {}, "ru", paid_access=True
        )
        await asyncio.sleep(0)
    assert handled is True
    assert ask.await_args.args[0] == OTHER


@pytest.mark.parametrize("paid_access, url", [(False, URL), (True, "")])
async def test_state_keeps_the_old_path_for_free_roles_or_when_switched_off(monkeypatch, paid_access, url):
    monkeypatch.setenv("INTERLOCUTOR_URL", url)
    state = make_state()
    ask = AsyncMock()
    with patch.object(ic, "ask", ask):
        handled = await state._answer_via_interlocutor(
            SimpleNamespace(), OTHER, "вопрос", {}, "ru", paid_access=paid_access
        )
    assert handled is False
    ask.assert_not_awaited()
    state.send.assert_not_awaited()  # not even the "thinking" message: nothing changes for them


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
    assert await _client_with_token(20).access_token_for(OWNER) == "fresh-pass"  # under a minute left


async def test_when_refresh_fails_the_stale_pass_is_not_handed_out():
    assert await _client_with_token(-30, refresh_ok=False).access_token_for(OWNER) is None


async def test_unknown_expiry_is_left_to_the_agent_and_a_missing_person_gives_nothing():
    assert await _client_with_token(None).access_token_for(OWNER) == "old-pass"
    assert await _client_with_token(3600).access_token_for(OTHER) is None  # not signed in, nothing to refresh
