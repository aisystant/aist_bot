import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core import nc_delivery

SECRET = base64.b64encode(bytearray(32)).decode()
ACCOUNT = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
ATTEMPT = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def request(extra_headers=None, body=None):
    payload = {
        "order_id": "cccccccc-3333-4333-8333-cccccccccccc",
        "attempt_id": ATTEMPT,
        "account_id": ACCOUNT,
        "token": "dG9rZW4",
        "kind": "test_owner_ping",
        "payload": {"title": "Untrusted", "body": "Request payload"},
        "scheduled_for": None,
    }
    if body:
        payload.update(body)
    raw = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    key_id = "nck_center_dispatcher"
    signature = hmac.new(
        bytearray(32), f"v1\n{key_id}\n{timestamp}\n".encode() + raw, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-NC-Key-Id": key_id,
        "X-NC-Timestamp": timestamp,
        "X-NC-Signature": signature,
        **(extra_headers or {}),
    }
    return SimpleNamespace(read=AsyncMock(return_value=raw), headers=headers)


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


def consumed():
    return {
        "attempt_id": ATTEMPT,
        "account_id": ACCOUNT,
        "kind": "test_owner_ping",
        "payload": {"title": "Authoritative", "body": "From center"},
        "scheduled_for": None,
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("NC_DELIVERY_SECRET", SECRET)
    monkeypatch.setenv("NC_CENTER_BASE_URL", "https://center.example")
    monkeypatch.setattr(nc_delivery._LIMITER, "admit", AsyncMock(return_value=0.0))


async def test_bad_signature_never_reaches_consume_or_telegram(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch("core.nc_delivery._consume", new=AsyncMock()) as consume, \
         patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))), \
         patch("oauth_server._bot_instance", bot):
        response = await nc_delivery.nc_deliver_handler(request({"X-NC-Signature": "0" * 64}))
    assert response.status == 401
    consume.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_success_uses_payload_returned_by_consume(env):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)))
    with patch("core.nc_delivery._consume", new=AsyncMock(return_value=consumed())), \
         patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))), \
         patch("oauth_server._bot_instance", bot):
        response = await nc_delivery.nc_deliver_handler(request())
    assert response.status == 200
    assert json.loads(response.body)["telegram_message_id"] == 123
    bot.send_message.assert_awaited_once_with(42, "Authoritative\n\nFrom center", parse_mode=None)


async def test_consume_refusal_is_terminal_and_does_not_call_telegram(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch("core.nc_delivery._consume", new=AsyncMock(return_value=None)), \
         patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))), \
         patch("oauth_server._bot_instance", bot):
        response = await nc_delivery.nc_deliver_handler(request())
    body = json.loads(response.body)
    assert body["outcome"] == "failed"
    assert body["retryable"] is False
    bot.send_message.assert_not_awaited()


async def test_missing_channel_does_not_consume_or_call_telegram(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch("core.nc_delivery._consume", new=AsyncMock()) as consume, \
         patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(None))), \
         patch("oauth_server._bot_instance", bot):
        response = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(response.body)["outcome"] == "no_channel"
    consume.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_consume_mismatch_does_not_call_telegram(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    wrong = {**consumed(), "account_id": "dddddddd-4444-4444-8444-dddddddddddd"}
    with patch("core.nc_delivery._consume", new=AsyncMock(return_value=wrong)), \
         patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))), \
         patch("oauth_server._bot_instance", bot):
        response = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(response.body)["reason"] == "consume_mismatch"
    bot.send_message.assert_not_awaited()


async def test_local_limiter_returns_429_before_consume(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch("core.nc_delivery._LIMITER.admit", new=AsyncMock(return_value=2.2)), \
         patch("core.nc_delivery._consume", new=AsyncMock()) as consume, \
         patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))), \
         patch("oauth_server._bot_instance", bot):
        response = await nc_delivery.nc_deliver_handler(request())
    assert response.status == 429
    assert response.headers["Retry-After"] == "3"
    consume.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_telegram_uncertainty_is_unknown(env):
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError("timeout")))
    with patch("core.nc_delivery._consume", new=AsyncMock(return_value=consumed())), \
         patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))), \
         patch("oauth_server._bot_instance", bot):
        response = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(response.body)["outcome"] == "unknown"


async def test_onboarding_lifecycle_kind_is_delivered_with_center_text(env):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=124)))
    center_reply = {**consumed(), "kind": "onboarding_lifecycle"}
    with patch("core.nc_delivery._consume", new=AsyncMock(return_value=center_reply)), \
         patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))), \
         patch("oauth_server._bot_instance", bot):
        response = await nc_delivery.nc_deliver_handler(request(body={"kind": "onboarding_lifecycle"}))
    assert response.status == 200
    assert json.loads(response.body)["telegram_message_id"] == 124
    bot.send_message.assert_awaited_once_with(42, "Authoritative\n\nFrom center", parse_mode=None)


async def test_invalid_kind_is_refused_before_consume(env):
    with patch("core.nc_delivery._consume", new=AsyncMock()) as consume:
        response = await nc_delivery.nc_deliver_handler(request(body={"kind": "live_unapproved"}))
    assert response.status == 400
    consume.assert_not_awaited()


def test_hmac_matches_typescript_golden_vector():
    assert nc_delivery._signature(
        bytes(range(32)), "nck_center_dispatcher", "1760000000", b'{"account_id":"a"}'
    ) == "7bcf8db2bdfab184f9f93778b40f503a34b361379130f24a60bb27dcb1a71456"


async def test_temporary_center_failure_is_retryable_and_does_not_call_telegram(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch("core.nc_delivery._consume", new=AsyncMock(return_value={"_transport_error": True})), \
         patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))), \
         patch("oauth_server._bot_instance", bot):
        response = await nc_delivery.nc_deliver_handler(request())
    assert response.status == 429
    assert json.loads(response.body)["source"] == "center"
    bot.send_message.assert_not_awaited()
