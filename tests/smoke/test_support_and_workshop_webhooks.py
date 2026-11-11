"""Inbound webhooks that message users: support replies (Chatwoot) and workshop payments.

Three regressions guarded here:
- a Chatwoot request without a valid signature must not reach any user;
- a resent workshop payment webhook must not send a second invite, but must still
  send it when the first delivery failed before the invite went out; a payment
  without payment_id (no idempotency key) must be rejected before it is recorded.

The Chatwoot handler is executed from source without importing bot startup
services, the same way as tests/smoke/test_internal_orphan_notify.py.
"""

import ast
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

import clients.chatwoot as chatwoot

SECRET = "synthetic-chatwoot-secret"
TIMESTAMP = str(int(time.time()))


def _sign(raw: bytes) -> str:
    signed_body = TIMESTAMP.encode() + b"." + raw
    return "sha256=" + hmac.new(SECRET.encode(), signed_body, hashlib.sha256).hexdigest()


# --- signature -----------------------------------------------------------


def test_signature_rejected_when_secret_not_configured(monkeypatch):
    monkeypatch.setattr(chatwoot, "CHATWOOT_WEBHOOK_SECRET", "")
    assert chatwoot.verify_signature(b"{}", "anything", TIMESTAMP) is False


def test_signature_rejected_when_header_missing(monkeypatch):
    monkeypatch.setattr(chatwoot, "CHATWOOT_WEBHOOK_SECRET", SECRET)
    assert chatwoot.verify_signature(b"{}", "", TIMESTAMP) is False


def test_signature_rejected_when_wrong(monkeypatch):
    monkeypatch.setattr(chatwoot, "CHATWOOT_WEBHOOK_SECRET", SECRET)
    assert chatwoot.verify_signature(b"{}", "0" * 64, TIMESTAMP) is False


def test_signature_accepted_when_valid(monkeypatch):
    monkeypatch.setattr(chatwoot, "CHATWOOT_WEBHOOK_SECRET", SECRET)
    assert chatwoot.verify_signature(b"{}", _sign(b"{}"), TIMESTAMP) is True


def test_signature_rejected_when_timestamp_missing_or_stale(monkeypatch):
    monkeypatch.setattr(chatwoot, "CHATWOOT_WEBHOOK_SECRET", SECRET)
    assert chatwoot.verify_signature(b"{}", _sign(b"{}"), "") is False
    assert chatwoot.verify_signature(b"{}", _sign(b"{}"), "invalid") is False
    assert chatwoot.verify_signature(b"{}", _sign(b"{}"), str(int(TIMESTAMP) - 3600)) is False


# --- support reply handler ---------------------------------------------


@pytest.fixture
def support(monkeypatch):
    monkeypatch.setattr(chatwoot, "CHATWOOT_WEBHOOK_SECRET", SECRET)
    helpdesk = ModuleType("db.queries.helpdesk")
    helpdesk.get_chat_id_by_conversation = AsyncMock(return_value=777)
    monkeypatch.setitem(sys.modules, "db.queries.helpdesk", helpdesk)

    source = Path(__file__).resolve().parents[2] / "oauth_server.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chatwoot_webhook_handler"
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    namespace = {"web": web, "json": json, "logger": logging.getLogger(__name__), "_bot_instance": bot}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["chatwoot_webhook_handler"], bot


async def _post(handler, body: dict, signature: str | None = "valid"):
    raw = json.dumps(body).encode()
    headers = {}
    if signature == "valid":
        headers["X-Chatwoot-Signature"] = _sign(raw)
        headers["X-Chatwoot-Timestamp"] = TIMESTAMP
    elif signature:
        headers["X-Chatwoot-Signature"] = signature
        headers["X-Chatwoot-Timestamp"] = TIMESTAMP
    request = SimpleNamespace(read=AsyncMock(return_value=raw), headers=headers)
    return await handler(request)


def _agent_reply(**overrides):
    body = {
        "event": "message_created",
        "message_type": "outgoing",
        "conversation": {"id": 16},
        "content": "Ответ оператора",
        "private": False,
        "sender": {"type": "user"},
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_public_agent_reply_is_forwarded(support):
    handler, bot = support
    response = await _post(handler, _agent_reply())
    assert response.status == 200
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 777
    assert "Ответ оператора" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_unsigned_request_is_rejected_and_not_forwarded(support):
    handler, bot = support
    response = await _post(handler, _agent_reply(), signature=None)
    assert response.status == 401
    bot.send_message.assert_not_awaited()


# --- workshop payment webhook ----------------------------------------------


@pytest.fixture
def workshop(monkeypatch):
    import handlers.workshop as ws

    create = AsyncMock(return_value=42)
    direct_invite = AsyncMock()
    count_invite = AsyncMock()
    monkeypatch.setattr(ws, "create_and_confirm_payment", create)
    monkeypatch.setattr(ws, "_send_direct_masterskaya_invite", direct_invite)
    monkeypatch.setattr(ws, "_send_invite_by_count", count_invite)
    monkeypatch.setattr(ws, "get_aisystant_id", AsyncMock(return_value=None))
    monkeypatch.setattr(ws, "get_intern", AsyncMock(return_value={"language": "ru"}))
    monkeypatch.setattr(ws, "get_workshop_payment_count", AsyncMock(return_value=1))
    sent_keys: set[str] = set()

    async def was_sent(key):
        return key in sent_keys

    async def record(chat_id, notification_type, key, **_):
        sent_keys.add(key)
        return True

    monkeypatch.setattr(ws, "was_notification_sent", was_sent)
    monkeypatch.setattr(ws, "try_insert_notification", record)
    dual_write = ModuleType("helpers.dual_write")
    dual_write.emit_payment_received = AsyncMock()
    monkeypatch.setitem(sys.modules, "helpers.dual_write", dual_write)
    return ws, create, count_invite, sent_keys


@pytest.mark.asyncio
async def test_first_payment_sends_invite_and_records_it(workshop):
    ws, create, invite, sent_keys = workshop
    result = await ws.process_workshop_webhook({"telegram_id": 5, "payment_id": "p-1"}, bot=None)
    assert result["ok"] is True
    create.assert_awaited_once()
    invite.assert_awaited_once()
    assert sent_keys == {"workshop_invite:p-1"}


@pytest.mark.asyncio
async def test_resent_payment_does_not_send_second_invite(workshop):
    ws, create, invite, _ = workshop
    await ws.process_workshop_webhook({"telegram_id": 5, "payment_id": "p-1"}, bot=None)
    create.return_value = 0  # payment_id already recorded
    result = await ws.process_workshop_webhook({"telegram_id": 5, "payment_id": "p-1"}, bot=None)
    assert result["ok"] is True
    assert invite.await_count == 1


@pytest.mark.asyncio
async def test_resent_payment_sends_invite_when_first_send_failed(workshop):
    ws, create, invite, sent_keys = workshop
    invite.side_effect = [RuntimeError("telegram down"), None]
    with pytest.raises(RuntimeError):
        await ws.process_workshop_webhook({"telegram_id": 5, "payment_id": "p-1"}, bot=None)
    assert sent_keys == set()
    create.return_value = 0  # payment recorded by the failed attempt
    await ws.process_workshop_webhook({"telegram_id": 5, "payment_id": "p-1"}, bot=None)
    assert invite.await_count == 2
    assert sent_keys == {"workshop_invite:p-1"}


@pytest.mark.asyncio
async def test_numeric_payment_id_is_passed_as_text(workshop):
    ws, create, _, _ = workshop
    await ws.process_workshop_webhook({"telegram_id": 5, "payment_id": 123}, bot=None)
    assert create.await_args.kwargs["payment_id"] == "123"


@pytest.mark.asyncio
async def test_payment_without_payment_id_is_rejected_before_recording(workshop):
    ws, create, invite, _ = workshop
    result = await ws.process_workshop_webhook({"telegram_id": 5}, bot=None)
    assert result == {"ok": False, "error": "missing payment_id"}
    create.assert_not_awaited()
    invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_without_id_returns_http_400(workshop, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "oauth_server.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    handler = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "workshop_payment_handler"
    )
    namespace = {
        "web": web,
        "os": os,
        "logger": logging.getLogger(__name__),
        "_bot_instance": object(),
    }
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"), namespace)
    monkeypatch.setenv("WORKSHOP_WEBHOOK_SECRET", "synthetic-payment-secret")
    request = SimpleNamespace(
        headers={"X-Webhook-Secret": "synthetic-payment-secret"},
        json=AsyncMock(return_value={"telegram_id": 5, "purpose": "WORKSHOP"}),
    )

    response = await namespace["workshop_payment_handler"](request)

    assert response.status == 400
    assert json.loads(response.text) == {"ok": False, "error": "missing payment_id"}
    workshop[1].assert_not_awaited()
