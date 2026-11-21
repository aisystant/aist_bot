from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any

import aiohttp
from aiohttp import web

from db.connection import get_persona_pool

_ALLOWED_KINDS = {"test_owner_ping", "test_platform_ping"}
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _signature(secret: bytes, key_id: str, timestamp: str, raw: bytes) -> str:
    return hmac.new(secret, f"v1\n{key_id}\n{timestamp}\n".encode() + raw, hashlib.sha256).hexdigest()


def _secret() -> bytes | None:
    value = os.getenv("NC_DELIVERY_SECRET", "").strip()
    if not value:
        return None
    try:
        key = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    return key if len(key) == 32 else None


class _LocalLimiter:
    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._global_at = 0.0
        self._chat_at: dict[int, float] = {}

    def retry_after(self, chat_id: int) -> float:
        if self._lock is None:
            self._lock = asyncio.Lock()
        now = time.monotonic()
        return max(0.0, self._global_at - now, self._chat_at.get(chat_id, 0.0) - now)

    async def admit(self, chat_id: int) -> float:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._global_at - now, self._chat_at.get(chat_id, 0.0) - now)
            if delay > 0:
                return delay
            self._global_at = now + 0.1
            self._chat_at[chat_id] = now + 1.0
            if len(self._chat_at) > 10_000:
                self._chat_at = {chat: at for chat, at in self._chat_at.items() if at > now}
            return 0.0


_LIMITER = _LocalLimiter()


def _reply(attempt_id: str, outcome: str, status: int = 200, **fields: Any) -> web.Response:
    retry_after = fields.get("retry_after")
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return web.json_response({"attempt_id": attempt_id, "outcome": outcome, **fields}, status=status, headers=headers)


async def _consume(account_id: str, attempt_id: str, token: str) -> dict[str, Any] | None:
    base_url = os.getenv("NC_CENTER_BASE_URL", "").strip().rstrip("/")
    secret = _secret()
    if not base_url or not secret:
        return {"_transport_error": True}
    raw = json.dumps({
        "account_id": account_id,
        "attempt_id": attempt_id,
        "token": token,
    }, separators=(",", ":")).encode()
    key_id = "nck_bot_mailman_0001"
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-NC-Key-Id": key_id,
        "X-NC-Timestamp": timestamp,
        "X-NC-Signature": _signature(secret, key_id, timestamp, raw),
    }
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base_url}/internal/nc/consume", data=raw, headers=headers) as response:
                body = await response.json()
                if response.status != 200:
                    return None if response.status in (400, 409) else {"_transport_error": True}
                return body
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return {"_transport_error": True}


async def nc_deliver_handler(request: web.Request) -> web.Response:
    raw = await request.read()
    secret = _secret()
    key_id = request.headers.get("X-NC-Key-Id", "")
    timestamp = request.headers.get("X-NC-Timestamp", "")
    provided = request.headers.get("X-NC-Signature", "")
    if (
        not secret
        or key_id != "nck_center_dispatcher"
        or not re.fullmatch(r"\d{10}", timestamp)
        or abs(int(time.time()) - int(timestamp)) > 300
        or not hmac.compare_digest(provided, _signature(secret, key_id, timestamp, raw))
    ):
        return web.json_response({"error": "signature_rejected"}, status=401)
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return web.json_response({"error": "invalid_request"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_request"}, status=400)
    attempt_id = body.get("attempt_id")
    account_id = body.get("account_id")
    if (
        not isinstance(attempt_id, str)
        or not _UUID_RE.fullmatch(attempt_id)
        or not isinstance(account_id, str)
        or not _UUID_RE.fullmatch(account_id)
        or body.get("kind") not in _ALLOWED_KINDS
        or not isinstance(body.get("token"), str)
    ):
        return web.json_response({"error": "invalid_request"}, status=400)

    account_id = account_id.lower()
    attempt_id = attempt_id.lower()
    from oauth_server import _bot_instance
    if _bot_instance is None:
        return _reply(attempt_id, "rate_limited", status=429, retry_after=5, source="bot_not_ready")
    pool = await get_persona_pool()
    async with pool.acquire() as conn:
        chat_id = await conn.fetchval(
            "SELECT telegram_id FROM public.ory_identity WHERE account_id = $1::uuid",
            account_id,
        )
    if chat_id is None:
        return _reply(attempt_id, "no_channel")
    delay = await _LIMITER.admit(chat_id)
    if delay > 0:
        return _reply(attempt_id, "rate_limited", status=429, retry_after=max(1, int(delay + 0.999)), source="local")

    consumed = await _consume(account_id, attempt_id, body["token"])
    if consumed and consumed.get("_transport_error"):
        return _reply(attempt_id, "rate_limited", status=429, retry_after=5, source="center")
    if not consumed:
        return _reply(attempt_id, "failed", phase="pre_send", reason="permit_not_consumed", retryable=False)
    if (
        consumed.get("attempt_id") != attempt_id
        or consumed.get("account_id") != account_id
        or consumed.get("kind") != body["kind"]
    ):
        return _reply(attempt_id, "failed", phase="pre_send", reason="consume_mismatch", retryable=False)
    payload = consumed.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("title"), str) or not isinstance(payload.get("body"), str):
        return _reply(attempt_id, "failed", phase="pre_send", reason="invalid_payload", retryable=False)
    text = f"{payload['title']}\n\n{payload['body']}"
    if isinstance(payload.get("link"), str):
        text += f"\n{payload['link']}"

    try:
        message = await _bot_instance.send_message(chat_id, text, parse_mode=None)
        return _reply(attempt_id, "sent", telegram_message_id=message.message_id)
    except Exception as error:
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramUnauthorizedError
        if isinstance(error, TelegramRetryAfter):
            return _reply(attempt_id, "rate_limited", status=429, retry_after=error.retry_after, source="telegram")
        if isinstance(error, TelegramForbiddenError):
            return _reply(attempt_id, "blocked")
        if isinstance(error, (TelegramBadRequest, TelegramUnauthorizedError)):
            return _reply(attempt_id, "failed", phase="telegram", reason=type(error).__name__, retryable=False)
        return _reply(attempt_id, "unknown", reason=type(error).__name__)
