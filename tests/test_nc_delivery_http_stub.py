import asyncio
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from core import nc_delivery

SECRET_BYTES = bytearray(32)
SECRET = base64.b64encode(SECRET_BYTES).decode()
ACCOUNT = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
ATTEMPT = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


class Pool:
    def __init__(self, chat_id):
        self.chat_id = chat_id

    def acquire(self):
        pool = self

        class Context:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def fetchval(self, *args):
                return pool.chat_id

        return Context()


async def start_server(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


class TelegramClient:
    def __init__(self, url, scenario):
        self.url = url
        self.scenario = scenario

    async def send_message(self, chat_id, text, parse_mode=None):
        timeout = aiohttp.ClientTimeout(total=0.05 if self.scenario == "unknown" else 5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}) as response:
                body = await response.json()
                if response.status == 200:
                    return SimpleNamespace(message_id=body["result"]["message_id"])
                if response.status == 429:
                    raise TelegramRetryAfter(method=None, message=body["description"], retry_after=body["parameters"]["retry_after"])
                if response.status == 403:
                    raise TelegramForbiddenError(method=None, message=body["description"])
                raise TelegramBadRequest(method=None, message=body["description"])


def signed_request():
    raw = json.dumps({
        "order_id": "cccccccc-3333-4333-8333-cccccccccccc",
        "attempt_id": ATTEMPT,
        "account_id": ACCOUNT,
        "token": "dG9rZW4",
        "kind": "test_owner_ping",
        "payload": {"title": "Untrusted", "body": "Request payload"},
        "scheduled_for": None,
    }).encode()
    timestamp = str(int(time.time()))
    key_id = "nck_center_dispatcher"
    signature = hmac.new(
        SECRET_BYTES, f"v1\n{key_id}\n{timestamp}\n".encode() + raw, hashlib.sha256
    ).hexdigest()
    return raw, {
        "Content-Type": "application/json",
        "X-NC-Key-Id": key_id,
        "X-NC-Timestamp": timestamp,
        "X-NC-Signature": signature,
    }


@pytest.mark.parametrize(
    "scenario,expected_status,expected_outcome",
    [
        ("sent", 200, "sent"),
        ("blocked", 200, "blocked"),
        ("no_channel", 200, "no_channel"),
        ("rate_limited", 429, "rate_limited"),
        ("failed", 200, "failed"),
        ("unknown", 200, "unknown"),
    ],
)
async def test_bot_delivery_against_http_stubs(monkeypatch, scenario, expected_status, expected_outcome):
    consume_calls = []
    telegram_calls = []
    consumed_tokens = set()
    center = web.Application()

    async def consume(request):
        body = await request.json()
        consume_calls.append(body)
        if body["token"] in consumed_tokens:
            return web.json_response({"error": "permit_not_consumed"}, status=409)
        consumed_tokens.add(body["token"])
        return web.json_response({
            "attempt_id": body["attempt_id"],
            "account_id": body["account_id"],
            "kind": "test_owner_ping",
            "payload": {"title": "Authoritative", "body": "From center"},
            "scheduled_for": None,
        })

    center.router.add_post("/internal/nc/consume", consume)
    telegram = web.Application()

    async def send_message(request):
        body = await request.json()
        telegram_calls.append(body)
        if scenario == "sent":
            return web.json_response({"ok": True, "result": {"message_id": 123}})
        if scenario == "blocked":
            return web.json_response({"ok": False, "description": "Forbidden: bot was blocked"}, status=403)
        if scenario == "rate_limited":
            return web.json_response({"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}, status=429)
        if scenario == "unknown":
            await asyncio.sleep(0.2)
        return web.json_response({"ok": False, "description": "Bad Request: chat not found"}, status=400)

    telegram.router.add_post("/sendMessage", send_message)
    bot = web.Application()
    bot.router.add_post("/internal/nc/deliver", nc_delivery.nc_deliver_handler)
    center_runner, center_url = await start_server(center)
    telegram_runner, telegram_url = await start_server(telegram)
    bot_runner, bot_url = await start_server(bot)
    monkeypatch.setenv("NC_DELIVERY_SECRET", SECRET)
    monkeypatch.setenv("NC_CENTER_BASE_URL", center_url)
    try:
        raw, headers = signed_request()
        with patch("core.nc_delivery._LIMITER.admit", new=AsyncMock(return_value=0.0)), \
             patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(None if scenario == "no_channel" else 42))), \
             patch("oauth_server._bot_instance", TelegramClient(telegram_url + "/sendMessage", scenario)):
            async with aiohttp.ClientSession() as session:
                async with session.post(bot_url + "/internal/nc/deliver", data=raw, headers=headers) as response:
                    reply = await response.json()
                    assert response.status == expected_status
        assert reply["outcome"] == expected_outcome
        if scenario == "sent":
            assert reply["telegram_message_id"] == 123
        if scenario == "rate_limited":
            assert reply["retry_after"] == 7
        if scenario == "unknown":
            await asyncio.sleep(0.3)
        expected_calls = 0 if scenario == "no_channel" else 1
        assert len(consume_calls) == expected_calls
        assert len(telegram_calls) == expected_calls
        if scenario == "sent":
            assert telegram_calls[0] == {"chat_id": 42, "text": "Authoritative\n\nFrom center", "parse_mode": None}
    finally:
        await bot_runner.cleanup()
        await telegram_runner.cleanup()
        await center_runner.cleanup()
