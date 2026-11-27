import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

from core import nc_delivery

SECRET = base64.b64encode(bytearray(32)).decode()
ACCOUNT = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
ATTEMPT = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
DELIVERED_AT = datetime(2026, 11, 27, 23, 59, 59, tzinfo=timezone.utc)


def telegram_message(message_id=123, date=DELIVERED_AT):
    return SimpleNamespace(message_id=message_id, date=date)


def center_http_response(status, body, headers=None):
    response = MagicMock()
    response.__aenter__.return_value = SimpleNamespace(
        status=status,
        headers=headers or {},
        json=AsyncMock(return_value=body),
    )
    session = MagicMock()
    session.post.return_value = response
    client = MagicMock()
    client.__aenter__.return_value = session
    return client, session.post


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
    signature = hmac.new(bytearray(32), f"v1\n{key_id}\n{timestamp}\n".encode() + raw, hashlib.sha256).hexdigest()
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


async def deliver_with_center_reply(status, body, headers=None):
    bot = SimpleNamespace(send_message=AsyncMock())
    client, post = center_http_response(status, body, headers)
    with (
        patch("core.nc_delivery.aiohttp.ClientSession", return_value=client),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    return response, bot.send_message, post


@pytest.mark.parametrize("kind", ["test_owner_ping", "marathon_reminders"])
async def test_bad_signature_never_reaches_consume_or_telegram(env, kind):
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch("core.nc_delivery._consume", new=AsyncMock()) as consume,
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request({"X-NC-Signature": "0" * 64}, body={"kind": kind}))
    assert response.status == 401
    assert "permit_consumed" not in json.loads(response.body)
    consume.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_success_uses_payload_returned_by_consume(env):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=telegram_message()))
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=consumed())),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    assert response.status == 200
    assert json.loads(response.body) == {
        "attempt_id": ATTEMPT,
        "outcome": "sent",
        "telegram_message_id": 123,
        "delivered_at": "2026-11-27T23:59:59Z",
        "permit_consumed": True,
    }
    bot.send_message.assert_awaited_once_with(42, "Authoritative\n\nFrom center", parse_mode=None)


@pytest.mark.parametrize(
    "date, expected",
    [
        (datetime(2026, 11, 28, 2, 59, 59, tzinfo=timezone(timedelta(hours=3))), "2026-11-27T23:59:59Z"),
        (datetime(2026, 11, 28, 0, 0, 0, tzinfo=timezone.utc), "2026-11-28T00:00:00Z"),
        (datetime(2026, 11, 27, 19, 0, 0, tzinfo=timezone(timedelta(hours=-5))), "2026-11-28T00:00:00Z"),
    ],
)
async def test_success_reports_telegram_time_in_utc_across_midnight(env, date, expected):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=telegram_message(date=date)))
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=consumed())),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(response.body)["delivered_at"] == expected
    assert json.loads(response.body)["permit_consumed"] is True
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(SimpleNamespace(message_id=123), id="missing"),
        pytest.param(telegram_message(date=None), id="null"),
        pytest.param(telegram_message(date=datetime(2026, 11, 27, 23, 59, 59)), id="naive"),
        pytest.param(telegram_message(date="2026-11-27T23:59:59Z"), id="string"),
        pytest.param(telegram_message(date=1_795_000_000), id="integer"),
    ],
)
async def test_invalid_telegram_time_after_send_is_unknown_without_retry(env, message):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message))
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=consumed())),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(response.body) == {
        "attempt_id": ATTEMPT,
        "outcome": "unknown",
        "reason": "invalid_delivery_time",
        "permit_consumed": True,
    }
    bot.send_message.assert_awaited_once()


async def test_replayed_attempt_does_not_send_again_after_timestamp_was_returned(env):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=telegram_message()))
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(side_effect=[consumed(), None])) as consume,
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        first = await nc_delivery.nc_deliver_handler(request())
        repeated = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(first.body)["delivered_at"] == "2026-11-27T23:59:59Z"
    assert json.loads(first.body)["permit_consumed"] is True
    assert json.loads(repeated.body)["reason"] == "permit_not_consumed"
    assert json.loads(repeated.body)["outcome"] == "unknown"
    assert "permit_consumed" not in json.loads(repeated.body)
    assert consume.await_count == 2
    bot.send_message.assert_awaited_once()


async def test_consume_refusal_is_unknown_and_does_not_call_telegram(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=None)),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    body = json.loads(response.body)
    assert body["outcome"] == "unknown"
    assert body["retryable"] is False
    assert "permit_consumed" not in body
    bot.send_message.assert_not_awaited()


async def test_missing_channel_does_not_consume_or_call_telegram(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch("core.nc_delivery._consume", new=AsyncMock()) as consume,
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(None))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(response.body)["outcome"] == "no_channel"
    assert "permit_consumed" not in json.loads(response.body)
    consume.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "mismatch",
    [
        {"account_id": "dddddddd-4444-4444-8444-dddddddddddd"},
        {"attempt_id": "dddddddd-4444-4444-8444-dddddddddddd"},
        {"kind": "marathon_reminders"},
    ],
)
async def test_consume_mismatch_does_not_call_telegram(env, mismatch):
    bot = SimpleNamespace(send_message=AsyncMock())
    wrong = {**consumed(), **mismatch}
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=wrong)),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(response.body)["reason"] == "consume_mismatch"
    assert "permit_consumed" not in json.loads(response.body)
    bot.send_message.assert_not_awaited()


async def test_local_limiter_returns_429_before_consume(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch("core.nc_delivery._LIMITER.admit", new=AsyncMock(return_value=2.2)),
        patch("core.nc_delivery._consume", new=AsyncMock()) as consume,
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    assert response.status == 429
    assert response.headers["Retry-After"] == "3"
    assert "permit_consumed" not in json.loads(response.body)
    consume.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_telegram_uncertainty_is_unknown(env):
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError("timeout")))
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=consumed())),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(response.body)["outcome"] == "unknown"
    assert json.loads(response.body)["permit_consumed"] is True


@pytest.mark.parametrize(
    "error, status, outcome",
    [
        (TelegramRetryAfter(method=None, message="slow down", retry_after=17), 429, "rate_limited"),
        (TelegramForbiddenError(method=None, message="bot blocked"), 200, "blocked"),
        (TelegramBadRequest(method=None, message="bad request"), 200, "failed"),
        (TelegramUnauthorizedError(method=None, message="token rejected"), 200, "failed"),
    ],
)
async def test_post_consume_telegram_error_proves_the_request_consumed_its_permit(env, error, status, outcome):
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=consumed())),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    body = json.loads(response.body)
    assert response.status == status
    assert body["outcome"] == outcome
    assert body["attempt_id"] == ATTEMPT
    assert body["permit_consumed"] is True
    if outcome == "rate_limited":
        assert response.headers["Retry-After"] == "17"
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("payload", [None, [], {"title": "missing body"}, {"title": 42, "body": "invalid title"}])
async def test_invalid_payload_after_matching_consume_proves_consumption_without_sending(env, payload):
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value={**consumed(), "payload": payload})),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    assert json.loads(response.body) == {
        "attempt_id": ATTEMPT,
        "outcome": "failed",
        "permit_consumed": True,
        "phase": "pre_send",
        "reason": "invalid_payload",
        "retryable": False,
    }
    bot.send_message.assert_not_awaited()


async def test_bot_not_ready_has_no_consumption_proof(env):
    with (
        patch("core.nc_delivery._consume", new=AsyncMock()) as consume,
        patch("oauth_server._bot_instance", None),
    ):
        response = await nc_delivery.nc_deliver_handler(request(body={"permit_consumed": True}))
    body = json.loads(response.body)
    assert response.status == 429
    assert body["source"] == "bot_not_ready"
    assert "permit_consumed" not in body
    consume.assert_not_awaited()


async def test_onboarding_lifecycle_kind_is_delivered_with_center_text(env):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=telegram_message(message_id=124)))
    center_reply = {**consumed(), "kind": "onboarding_lifecycle"}
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=center_reply)),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request(body={"kind": "onboarding_lifecycle"}))
    assert response.status == 200
    assert json.loads(response.body)["telegram_message_id"] == 124
    bot.send_message.assert_awaited_once_with(42, "Authoritative\n\nFrom center", parse_mode=None)


async def test_a_kind_authorized_by_center_is_delivered_without_local_kind_policy(env):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=telegram_message()))
    center_reply = {**consumed(), "kind": "marathon_reminders"}
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=center_reply)) as consume,
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(
            request(body={"kind": "marathon_reminders", "delivery_place": "f" * 64})
        )
    assert json.loads(response.body)["outcome"] == "sent"
    consume.assert_awaited_once_with(ACCOUNT, ATTEMPT, "dG9rZW4", 42)
    bot.send_message.assert_awaited_once_with(42, "Authoritative\n\nFrom center", parse_mode=None)


async def test_unknown_kind_refused_by_center_never_reaches_telegram(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value=None)),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request(body={"kind": "live_unapproved"}))
    assert json.loads(response.body)["reason"] == "permit_not_consumed"
    assert json.loads(response.body)["outcome"] == "unknown"
    assert "permit_consumed" not in json.loads(response.body)
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("kind", [None, 42, "bad-kind", "UPPERCASE", "a", "a" * 64])
async def test_invalid_kind_is_refused_before_consume(env, kind):
    with patch("core.nc_delivery._consume", new=AsyncMock()) as consume:
        response = await nc_delivery.nc_deliver_handler(request(body={"kind": kind}))
    assert response.status == 400
    assert "permit_consumed" not in json.loads(response.body)
    consume.assert_not_awaited()


def test_hmac_matches_typescript_golden_vector():
    assert (
        nc_delivery._signature(bytes(range(32)), "nck_center_dispatcher", "1760000000", b'{"account_id":"a"}')
        == "7bcf8db2bdfab184f9f93778b40f503a34b361379130f24a60bb27dcb1a71456"
    )


async def test_consume_signs_the_place_derived_from_resolved_chat(env):
    client, post = center_http_response(200, consumed())
    with patch("core.nc_delivery.aiohttp.ClientSession", return_value=client):
        assert await nc_delivery._consume(ACCOUNT, ATTEMPT, "dG9rZW4", 42) == consumed()
    args, kwargs = post.call_args
    assert args == ("https://center.example/internal/nc/consume",)
    raw = kwargs["data"]
    sent = json.loads(raw)
    assert sent["account_id"] == ACCOUNT
    assert sent["attempt_id"] == ATTEMPT
    assert sent["delivery_place"] == hashlib.sha256(b"notification-center:telegram:v1:42").hexdigest()
    headers = kwargs["headers"]
    expected = nc_delivery._signature(bytes(32), headers["X-NC-Key-Id"], headers["X-NC-Timestamp"], raw)
    assert headers["X-NC-Signature"] == expected
    changed = json.dumps({**sent, "delivery_place": "f" * 64}, separators=(",", ":")).encode()
    assert nc_delivery._signature(bytes(32), headers["X-NC-Key-Id"], headers["X-NC-Timestamp"], changed) != expected


@pytest.mark.parametrize("header", ["1", "43200", "86400"])
async def test_center_cap_preserves_retry_until_next_utc_day_without_sending(env, header):
    response, send, post = await deliver_with_center_reply(
        429, {"error": "daily_cap_exceeded"}, {"Retry-After": header}
    )
    assert response.status == 429
    assert response.headers["Retry-After"] == header
    assert json.loads(response.body) == {
        "attempt_id": ATTEMPT,
        "outcome": "rate_limited",
        "retry_after": int(header),
        "source": "center_cap",
    }
    post.assert_called_once()
    send.assert_not_awaited()


@pytest.mark.parametrize(
    "error, header",
    [
        ("daily_cap_exceeded", None),
        ("daily_cap_exceeded", "0"),
        ("daily_cap_exceeded", "86401"),
        ("daily_cap_exceeded", "1.5"),
        ("daily_cap_exceeded", "-1"),
        ("daily_cap_exceeded", "tomorrow"),
        ("temporary_overload", "43200"),
    ],
)
async def test_other_center_limits_use_short_transport_retry_without_sending(env, error, header):
    response, send, post = await deliver_with_center_reply(429, {"error": error}, {"Retry-After": header})
    assert response.status == 429
    assert response.headers["Retry-After"] == "5"
    assert json.loads(response.body)["source"] == "center"
    post.assert_called_once()
    send.assert_not_awaited()


@pytest.mark.parametrize("body", [[], ["unexpected"], "unexpected", False, True, None, 42])
async def test_non_object_successful_consume_reply_is_a_transport_failure_without_sending(env, body):
    response, send, post = await deliver_with_center_reply(200, body)
    assert response.status == 429
    assert response.headers["Retry-After"] == "5"
    assert json.loads(response.body) == {
        "attempt_id": ATTEMPT,
        "outcome": "rate_limited",
        "retry_after": 5,
        "source": "center",
    }
    post.assert_called_once()
    send.assert_not_awaited()


async def test_consume_409_is_unknown_without_ownership_or_telegram_send(env):
    response, send, post = await deliver_with_center_reply(409, {"error": "permit_not_consumed"})
    assert json.loads(response.body) == {
        "attempt_id": ATTEMPT,
        "outcome": "unknown",
        "phase": "pre_send",
        "reason": "permit_not_consumed",
        "retryable": False,
    }
    post.assert_called_once()
    send.assert_not_awaited()


async def test_temporary_center_failure_is_retryable_and_does_not_call_telegram(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch("core.nc_delivery._consume", new=AsyncMock(return_value={"_transport_error": True})),
        patch("core.nc_delivery.get_persona_pool", new=AsyncMock(return_value=Pool(42))),
        patch("oauth_server._bot_instance", bot),
    ):
        response = await nc_delivery.nc_deliver_handler(request())
    assert response.status == 429
    assert json.loads(response.body)["source"] == "center"
    bot.send_message.assert_not_awaited()
