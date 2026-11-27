from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web

from db.connection import get_persona_pool

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
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


def _consumed_reply(attempt_id: str, outcome: str, status: int = 200, **fields: Any) -> web.Response:
    """Only the handler that consumed and verified this attempt may prove it."""
    return _reply(attempt_id, outcome, status, permit_consumed=True, **fields)


def _telegram_delivered_at(message: Any) -> str | None:
    """Use Telegram's timestamp, never the queue or this process's clock."""
    delivered_at = getattr(message, "date", None)
    if not isinstance(delivered_at, datetime) or delivered_at.utcoffset() is None:
        return None
    return delivered_at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cap_retry_after(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"[0-9]{1,5}", value):
        return None
    seconds = int(value)
    return seconds if 1 <= seconds <= 86_400 else None


async def _consume(account_id: str, attempt_id: str, token: str, chat_id: int) -> dict[str, Any] | None:
    base_url = os.getenv("NC_CENTER_BASE_URL", "").strip().rstrip("/")
    secret = _secret()
    if not base_url or not secret:
        return {"_transport_error": True}
    raw = json.dumps(
        {
            "account_id": account_id,
            "attempt_id": attempt_id,
            "token": token,
            "delivery_place": hashlib.sha256(b"notification-center:telegram:v1:" + str(chat_id).encode()).hexdigest(),
        },
        separators=(",", ":"),
    ).encode()
    key_id = "nck_botmailman000001"
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
                if response.status == 429 and isinstance(body, dict) and body.get("error") == "daily_cap_exceeded":
                    retry_after = _cap_retry_after(response.headers.get("Retry-After"))
                    if retry_after is not None:
                        return {"_cap_exceeded": True, "retry_after": retry_after}
                if response.status != 200:
                    return None if response.status in (400, 409) else {"_transport_error": True}
                return body if isinstance(body, dict) else {"_transport_error": True}
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
    kind = body.get("kind")
    if (
        not isinstance(attempt_id, str)
        or not _UUID_RE.fullmatch(attempt_id)
        or not isinstance(account_id, str)
        or not _UUID_RE.fullmatch(account_id)
        or not isinstance(kind, str)
        or not _KIND_RE.fullmatch(kind)
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

    consumed = await _consume(account_id, attempt_id, body["token"], chat_id)
    if consumed and consumed.get("_cap_exceeded"):
        return _reply(attempt_id, "rate_limited", status=429, retry_after=consumed["retry_after"], source="center_cap")
    if consumed and consumed.get("_transport_error"):
        return _reply(attempt_id, "rate_limited", status=429, retry_after=5, source="center")
    if not consumed:
        return _reply(attempt_id, "unknown", phase="pre_send", reason="permit_not_consumed", retryable=False)
    if (
        consumed.get("attempt_id") != attempt_id
        or consumed.get("account_id") != account_id
        or consumed.get("kind") != kind
    ):
        return _reply(attempt_id, "failed", phase="pre_send", reason="consume_mismatch", retryable=False)
    payload = consumed.get("payload")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("title"), str)
        or not isinstance(payload.get("body"), str)
    ):
        return _consumed_reply(attempt_id, "failed", phase="pre_send", reason="invalid_payload", retryable=False)
    text = f"{payload['title']}\n\n{payload['body']}"
    if isinstance(payload.get("link"), str):
        text += f"\n{payload['link']}"

    try:
        message = await _bot_instance.send_message(chat_id, text, parse_mode=None)
        delivered_at = _telegram_delivered_at(message)
        if delivered_at is None:
            return _consumed_reply(attempt_id, "unknown", reason="invalid_delivery_time")
        return _consumed_reply(attempt_id, "sent", telegram_message_id=message.message_id, delivered_at=delivered_at)
    except Exception as error:
        from aiogram.exceptions import (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramRetryAfter,
            TelegramUnauthorizedError,
        )

        if isinstance(error, TelegramRetryAfter):
            return _consumed_reply(
                attempt_id, "rate_limited", status=429, retry_after=error.retry_after, source="telegram"
            )
        if isinstance(error, TelegramForbiddenError):
            return _consumed_reply(attempt_id, "blocked")
        if isinstance(error, (TelegramBadRequest, TelegramUnauthorizedError)):
            return _consumed_reply(attempt_id, "failed", phase="telegram", reason=type(error).__name__, retryable=False)
        return _consumed_reply(attempt_id, "unknown", reason=type(error).__name__)
