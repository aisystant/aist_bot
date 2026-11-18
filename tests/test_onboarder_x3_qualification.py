"""Х3 offer for «Работник» and above (WP-417 Ф8, pilot 18.11.2026: option 2A, reading rule A)."""
import asyncio
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("KNOWLEDGE_MCP_URL", "https://fake-mcp.test/mcp")
os.environ.setdefault("USE_STATE_MACHINE", "false")

import pytest

from core.onboarder import x3

REFORMER = {"level": "Реформатор", "numeric": 70}
STUDENT = {"level": "Ученик", "numeric": 20}


class _Gateway:
    """Gateway stub: replays degree reads; an Exception item raises, a str item is an error code."""

    def __init__(self, reads, connected=True):
        self.reads = list(reads)
        self.connected = connected
        self.last_call_error = "ok"
        self.calls = 0

    def is_connected(self, chat_id):
        return self.connected

    async def dt_read_degree(self, chat_id):
        self.calls += 1
        item = self.reads.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            self.last_call_error = item
            return None
        self.last_call_error = "ok"
        return item


@pytest.fixture
def gateway(monkeypatch):
    def install(reads, connected=True):
        stub = _Gateway(reads, connected)
        # the clients package re-exports the client object under the module's name
        monkeypatch.setattr(importlib.import_module("clients.gateway_mcp"), "gateway_mcp", stub)
        return stub
    return install


@pytest.mark.parametrize("reads, expected, calls", [
    ([REFORMER], (x3.QUALIFIED, "Реформатор"), 1),
    ([STUDENT], (x3.LEARNER, None), 1),
    ([None], (x3.LEARNER, None), 1),                                   # read fine, no qualification
    (["rpc_error", REFORMER], (x3.QUALIFIED, "Реформатор"), 2),        # one retry
    (["rpc_error", "rpc_error"], (x3.UNKNOWN, None), 2),
    ([asyncio.TimeoutError()], (x3.UNKNOWN, None), 1),                 # no retry after a timeout
    (["token_expired"], (x3.LEARNER, None), 1),                        # lost login = not connected
    (["not_authorized"], (x3.LEARNER, None), 1),
    ([{"level": "Реформатор", "numeric": None}], (x3.UNKNOWN, None), 1),  # no usable level number
])
async def test_read_onboarding_qualification(gateway, reads, expected, calls):
    stub = gateway(reads)
    assert await x3.read_onboarding_qualification(42) == expected
    assert stub.calls == calls


async def test_no_twin_connected_is_the_learner_path_without_a_read(gateway):
    stub = gateway([], connected=False)
    assert await x3.read_onboarding_qualification(42) == (x3.LEARNER, None)
    assert stub.calls == 0


def _bot():
    return SimpleNamespace(send_message=AsyncMock())


async def test_qualified_offer_is_the_programme_by_qualification():
    bot = _bot()
    await x3._show_qualified_offer(bot, 42, "Реформатор", from_bridge=False)

    text = bot.send_message.await_args.args[1]
    assert "Реформатор" in text and "Исследовательское развитие" in text and "Личное развитие" not in text
    button = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "x3_confirm:QUAL:0"


async def test_qualification_without_a_known_programme_goes_to_a_mentor():
    bot = _bot()
    await x3._show_qualified_offer(bot, 42, "Неизвестная <степень>", from_bridge=True)

    text = bot.send_message.await_args.args[1]
    assert "наставник" in text and "&lt;степень&gt;" in text
    assert bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "x3_confirm:MENTOR:1"


async def test_run_x3_offers_no_learner_stage_to_a_qualified_person(gateway, monkeypatch):
    gateway([REFORMER])
    cp_read = AsyncMock()
    monkeypatch.setattr("db.queries.cp_assessment.get_latest_cp_assessment", cp_read)
    message = SimpleNamespace(bot=_bot(), chat=SimpleNamespace(id=42), answer=AsyncMock())

    await x3.run_x3({"chat_id": 42}, message)

    assert "x3_confirm:QUAL:0" in str(message.bot.send_message.await_args.kwargs["reply_markup"])
    cp_read.assert_not_awaited()


async def test_run_x3_unreadable_qualification_gives_a_neutral_offer_and_keeps_the_bridge(gateway, monkeypatch):
    gateway(["rpc_error", "rpc_error"])
    saved = AsyncMock()
    monkeypatch.setattr("core.onboarder.storage.save_onboarding_context", saved)
    message = SimpleNamespace(bot=_bot(), chat=SimpleNamespace(id=42), answer=AsyncMock())

    await x3.run_x3({"chat_id": 42}, message)

    text = message.bot.send_message.await_args.args[1]
    assert "/diagnose" in text and "S1" not in text and "этап не предлагаю" in text
    assert saved.await_args.args[1]["return_to"] == x3._RETURN_TO_X3


def test_describe_stream_for_qualification_offers():
    assert x3.describe_stream(x3.QUALIFICATION_STREAM) == "программа по квалификации"
    assert x3.describe_stream(x3.MENTOR_STREAM) == "подбор программы с наставником"
    assert x3.describe_stream("S2").startswith("этап")


async def test_failed_primary_read_is_not_hidden_by_an_empty_fallback_read():
    """dt_read_degree: 3_8_degree read fails, the LMS fallback reads fine but empty → the failure code stays."""
    gateway_module = importlib.import_module("clients.gateway_mcp")
    client = gateway_module.GatewayMCPClient.__new__(gateway_module.GatewayMCPClient)
    codes = iter(["rpc_error", "ok"])

    async def fake_call(*args, **kwargs):
        gateway_module._last_call_error.set(next(codes))
        return None

    client._call = fake_call
    client._parse_text_content = lambda result: None

    assert await client.dt_read_degree(42) is None
    assert gateway_module._last_call_error.get() == "rpc_error"
