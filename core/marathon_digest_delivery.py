"""Pin the weekly digest route in the journal shared with the legacy sender."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import aiohttp

from core.nc_delivery import _signature
from db.connection import get_persona_pool
from db.queries.notifications import (
    get_marathon_digest_route,
    insert_domain_event_direct,
    try_insert_notification,
)


class DigestDeliveryError(RuntimeError):
    """A transport refusal or unavailable route. Never includes user data."""


@dataclass(frozen=True)
class DigestCenter:
    base_url: str
    key_id: str
    secret: bytes
    sender_id: str

    @classmethod
    def load(cls) -> DigestCenter | None:
        if os.getenv("NC_MARATHON_DIGEST_ENABLED") != "true":
            return None
        base_url = os.getenv("NC_CENTER_BASE_URL", "").rstrip("/")
        key_id = os.getenv("NC_MARATHON_DIGEST_KEY_ID", "")
        encoded = os.getenv("NC_MARATHON_DIGEST_SECRET", "")
        sender_id = os.getenv("NC_MARATHON_DIGEST_SENDER_ID", "")
        if not (
            base_url.startswith("https://")
            and re.fullmatch(r"nck_[A-Za-z0-9]{16,64}", key_id)
            and re.fullmatch(r"[A-Za-z0-9_-]{43}", encoded)
        ):
            raise DigestDeliveryError("digest Center configuration is incomplete")
        try:
            sender_id = str(UUID(sender_id))
        except ValueError as exc:
            raise DigestDeliveryError("digest Center sender UUID is invalid") from exc
        return cls(base_url, key_id, base64.urlsafe_b64decode(encoded + "="), sender_id)

    async def _post(self, path: str, body: dict) -> tuple[int, dict]:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-NC-Key-Id": self.key_id,
            "X-NC-Timestamp": timestamp,
            "X-NC-Signature": _signature(self.secret, self.key_id, timestamp, raw),
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as session:
                async with session.post(
                    self.base_url + path, data=raw, headers=headers, allow_redirects=False
                ) as response:
                    result = await response.json()
                    if isinstance(result, dict):
                        return response.status, result
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise DigestDeliveryError("digest Center response unavailable") from exc
        raise DigestDeliveryError("invalid digest Center response")

    async def route(self, account_id: str) -> str:
        status, body = await self._post("/internal/marathon-digest/route", {"recipient": account_id})
        route = body.get("route")
        if status == 200 and body.get("sender_id") == self.sender_id and route in ("center", "legacy", "suppress"):
            return route
        raise DigestDeliveryError("digest route could not be determined")

    async def submit(self, week: str, account_id: str, title: str, body: str) -> None:
        status, reply = await self._post(
            "/v1/releases",
            {
                "account_id": account_id,
                "kind": "digest",
                "release_key": f"marathon-weekly:{week}",
                "payload": {"title": title, "body": body},
            },
        )
        if status in (200, 201) and isinstance(reply.get("release_id"), str):
            return
        if (status, reply.get("error")) in (
            (409, "release_key_conflict"),
            (422, "kind_disabled"),
            (422, "not_subscribed"),
        ):
            # The dedicated producer uses one key per recipient and week.
            # A conflict proves that this week's release already exists. A
            # later opt-out terminates this route without a direct fallback.
            return
        raise DigestDeliveryError("digest release was not confirmed")


async def _linked_account(chat_id: int) -> str | None:
    pool = await get_persona_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT account_id FROM public.ory_identity WHERE telegram_id = $1 LIMIT 2",
        chat_id,
    )
    return str(rows[0]["account_id"]) if len(rows) == 1 else None


async def plan_digest(
    center: DigestCenter | None, chat_id: int, week: str, *, retry: bool = False
) -> tuple[str, str | None] | None:
    key = f"marathon_digest:{chat_id}:{week}"
    if center is None:
        if retry:
            return None
        return ("legacy", None) if await try_insert_notification(chat_id, "marathon_digest", key) else None

    existing = await get_marathon_digest_route(key)
    if existing:
        if existing["route"] != "center" or existing["finished"]:
            return None
        account = await _linked_account(chat_id)
        if account is None or UUID(account) != existing["account_id"]:
            raise DigestDeliveryError("digest account link has changed")
        if center.sender_id != existing["sender_id"]:
            raise DigestDeliveryError("digest sender has changed")
        return "center", account

    account = await _linked_account(chat_id)
    route = await center.route(account) if account else "legacy"
    if route == "legacy":
        return ("legacy", None) if await try_insert_notification(chat_id, "marathon_digest", key) else None

    pinned = await insert_domain_event_direct(
        "aist-bot",
        f"notification-{key}",
        "marathon_digest_route",
        "v1",
        datetime.now(timezone.utc),
        account,
        {"route": route, "sender_id": center.sender_id},
    )
    return ("center", account) if pinned and route == "center" else None


async def finish_digest(chat_id: int, week: str, account_id: str) -> None:
    await insert_domain_event_direct(
        "aist-bot",
        f"marathon-digest-completed:{chat_id}:{week}",
        "marathon_digest_center_complete",
        "v1",
        datetime.now(timezone.utc),
        account_id,
        {},
    )
