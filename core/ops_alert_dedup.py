"""Single-flight delivery and short-lived dedup for operator alerts."""

import asyncio
import hashlib
import json
import time

_LAST_SENT: dict[str, float] = {}
_PENDING: dict[str, asyncio.Event] = {}


async def _reserve(key: str, ttl_seconds: int) -> tuple[bool, str, asyncio.Event | None]:
    while True:
        now = time.time()
        previous = _LAST_SENT.get(key)
        if previous is not None and now - previous < ttl_seconds:
            return False, key, None
        pending = _PENDING.get(key)
        if pending is not None:
            await pending.wait()
            continue
        reservation = asyncio.Event()
        _PENDING[key] = reservation
        return True, key, reservation


def _finish(key: str, reservation: asyncio.Event, sent: bool) -> None:
    if _PENDING.get(key) is not reservation:
        return
    if sent:
        _LAST_SENT[key] = time.time()
    _PENDING.pop(key, None)
    reservation.set()


async def reserve_reindex_alert(body: dict) -> tuple[bool, str, asyncio.Event | None]:
    failed = body.get("failed", [])
    entries = [entry for entry in failed if isinstance(entry, dict)] if isinstance(failed, list) else []
    serialized = json.dumps(entries, sort_keys=True, default=str)
    if entries and all("OpenRouter embeddings error: 401" in str(entry.get("error", "")) for entry in entries):
        key = f"reindex-openrouter-401:{time.strftime('%Y-%m-%d', time.gmtime())}"
        return await _reserve(key, 86_400)
    source = str(body.get("source", "?"))
    fingerprint = hashlib.sha256(serialized.encode()).hexdigest()[:16]
    bucket = int(time.time() // 3600)
    key = f"reindex:{source}:{bucket}:{fingerprint}"
    return await _reserve(key, 3600)


async def reserve_installation_orphaned(body: dict) -> tuple[bool, str, asyncio.Event | None]:
    installation_id = str(body.get("installation_id", "?"))
    key = f"installation-orphaned:{installation_id}:{time.strftime('%Y-%m-%d', time.gmtime())}"
    return await _reserve(key, 86_400)


def mark_alert_sent(key: str, reservation: asyncio.Event) -> None:
    _finish(key, reservation, True)


def release_reservation(key: str, reservation: asyncio.Event) -> None:
    _finish(key, reservation, False)


def reset_ops_alert_dedup() -> None:
    _LAST_SENT.clear()
    for pending in _PENDING.values():
        pending.set()
    _PENDING.clear()
