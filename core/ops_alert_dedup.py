"""Single-flight delivery and daily dedup for installation alerts."""

import asyncio
import time

_MAX_WINDOW_SECONDS = 86_400
_LAST_SENT: dict[str, float] = {}
_PENDING: dict[str, asyncio.Event] = {}


async def _reserve(key: str) -> tuple[bool, str, asyncio.Event | None]:
    while True:
        now = time.time()
        for old in [
            key for key, sent in _LAST_SENT.items() if now - sent >= _MAX_WINDOW_SECONDS
        ]:
            del _LAST_SENT[old]
        previous = _LAST_SENT.get(key)
        if previous is not None and now - previous < _MAX_WINDOW_SECONDS:
            return False, key, None
        pending = _PENDING.get(key)
        if pending is not None:
            await pending.wait()
            continue
        reservation = asyncio.Event()
        _PENDING[key] = reservation
        return True, key, reservation


async def reserve_installation_orphaned(
    body: dict,
) -> tuple[bool, str, asyncio.Event | None]:
    installation_id = str(body.get("installation_id", "?"))
    key = f"installation-orphaned:{installation_id}:{time.strftime('%Y-%m-%d', time.gmtime())}"
    return await _reserve(key)


def mark_alert_sent(key: str, reservation: asyncio.Event) -> None:
    if _PENDING.get(key) is reservation:
        _LAST_SENT[key] = time.time()
        _PENDING.pop(key)
        reservation.set()


def release_reservation(key: str, reservation: asyncio.Event) -> None:
    if _PENDING.get(key) is reservation:
        _PENDING.pop(key)
        reservation.set()


def reset_ops_alert_dedup() -> None:
    _LAST_SENT.clear()
    for pending in _PENDING.values():
        pending.set()
    _PENDING.clear()
