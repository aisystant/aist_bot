"""Single-flight delivery and short-lived dedup for operator alerts."""

import asyncio
import hashlib
import json
import time

from core.reindex_alert import is_unknown_result

# An unknown reindex result repeats with every push that times out; the old hourly bucket let
# the same source through again after each hour boundary (WP-568, 16.11: four near-identical
# chat messages in two hours). One message per source per window, repeats are only counted.
UNKNOWN_RESULT_WINDOW_SECONDS = 6 * 3600

_LAST_SENT: dict[str, float] = {}
_PENDING: dict[str, asyncio.Event] = {}
_SUPPRESSED: dict[str, int] = {}


async def _reserve(key: str, ttl_seconds: int) -> tuple[bool, str, asyncio.Event | None]:
    while True:
        now = time.time()
        previous = _LAST_SENT.get(key)
        if previous is not None and now - previous < ttl_seconds:
            _SUPPRESSED[key] = _SUPPRESSED.get(key, 0) + 1
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
    if entries and all(is_unknown_result(entry.get("error", "")) for entry in entries):
        return await _reserve(f"reindex-unknown:{source}", UNKNOWN_RESULT_WINDOW_SECONDS)
    fingerprint = hashlib.sha256(serialized.encode()).hexdigest()[:16]
    bucket = int(time.time() // 3600)
    key = f"reindex:{source}:{bucket}:{fingerprint}"
    return await _reserve(key, 3600)


async def reserve_installation_orphaned(body: dict) -> tuple[bool, str, asyncio.Event | None]:
    installation_id = str(body.get("installation_id", "?"))
    key = f"installation-orphaned:{installation_id}:{time.strftime('%Y-%m-%d', time.gmtime())}"
    return await _reserve(key, 86_400)


def suppressed_count(key: str) -> int:
    """Repeats swallowed for this key since its last delivered message."""
    return _SUPPRESSED.get(key, 0)


def mark_alert_sent(key: str, reservation: asyncio.Event) -> None:
    _finish(key, reservation, True)
    _SUPPRESSED.pop(key, None)


def release_reservation(key: str, reservation: asyncio.Event) -> None:
    _finish(key, reservation, False)


def reset_ops_alert_dedup() -> None:
    _LAST_SENT.clear()
    _SUPPRESSED.clear()
    for pending in _PENDING.values():
        pending.set()
    _PENDING.clear()
