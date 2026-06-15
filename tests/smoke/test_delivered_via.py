"""Smoke-тесты WP-418: контракт delivered_via (PR #286).

Три точки наблюдения:
  1. drain() передаёт delivered_via='notification_service' в try_insert_notification.
  2. try_insert_notification кладёт его в JSONB-payload; при None — поля нет.
  3. Round-trip: вставленное событие видит was_notification_sent.

FakeLearningConn / patched_learning — в conftest.py.
"""
import json

import pytest

import core.notification_service as ns
from db.queries.notifications import try_insert_notification, was_notification_sent


# ─── Fake для development.notification_queue (только drain, нужен только здесь) ───

class _DevConn:
    """Минимальный fake для development.notification_queue."""

    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *a):
                return False

        return _Tx()

    async def fetch(self, sql, *args):
        return self._rows

    async def execute(self, sql, *args):
        self.updates.append(args)
        return "OK"


class _DevPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False

        return _Acq()


# ─── Тест 1: drain stamps delivered_via ───

@pytest.mark.asyncio
async def test_drain_stamps_delivered_via(monkeypatch):
    """drain() вызывает try_insert_notification с delivered_via='notification_service'."""
    row = {
        "id": 1,
        "chat_id": 555,
        "notification_class": ns.CLASS_CAPPED,
        "payload": json.dumps({"text": "hello"}),
        "priority": 4,
        "journal_key": "nudge:555:2026-06-15:test",
        "journal_type": "nudge",
    }

    async def _fake_get_pool():
        return _DevPool(_DevConn([row]))

    monkeypatch.setattr(ns, "get_pool", _fake_get_pool)

    journaled = []

    async def _capture(**kwargs):
        journaled.append(kwargs)
        return True

    monkeypatch.setattr(ns, "try_insert_notification", _capture)

    async def _noop_deliver(chat_id, content):
        pass

    await ns.drain(_noop_deliver)

    assert len(journaled) == 1, "drain должен журналировать ровно одну строку"
    assert journaled[0]["delivered_via"] == "notification_service"


# ─── Тест 2: try_insert_notification sets delivered_via in payload ───

@pytest.mark.asyncio
async def test_try_insert_sets_delivered_via_in_payload(patched_learning):
    """try_insert_notification пишет delivered_via в JSONB; при None — поле отсутствует."""
    # С delivered_via
    ok = await try_insert_notification(
        chat_id=555,
        notification_type="nudge",
        idempotency_key="nudge:555:2026-06-15:with_marker",
        delivered_via="notification_service",
    )
    assert ok is True
    stored = patched_learning._store.get("notification-nudge:555:2026-06-15:with_marker")
    assert stored is not None, "Запись должна попасть в _store"
    assert stored.get("delivered_via") == "notification_service"

    # Без delivered_via → поля нет
    ok2 = await try_insert_notification(
        chat_id=555,
        notification_type="nudge",
        idempotency_key="nudge:555:2026-06-15:no_marker",
    )
    assert ok2 is True
    stored2 = patched_learning._store.get("notification-nudge:555:2026-06-15:no_marker")
    assert stored2 is not None, "Запись без delivered_via тоже должна попасть в _store"
    assert "delivered_via" not in stored2


# ─── Тест 3: round-trip ───

@pytest.mark.asyncio
async def test_delivered_via_round_trip(patched_learning):
    """Round-trip: was_notification_sent видит событие после try_insert_notification."""
    ikey = "nudge:555:2026-06-15:roundtrip"

    # До вставки — запись отсутствует
    assert await was_notification_sent(ikey) is False

    # Вставляем с delivered_via
    inserted = await try_insert_notification(
        chat_id=555,
        notification_type="nudge",
        idempotency_key=ikey,
        delivered_via="notification_service",
    )
    assert inserted is True

    # После вставки — was_notification_sent видит запись
    assert await was_notification_sent(ikey) is True

    # Payload содержит маркер
    stored = patched_learning._store.get(f"notification-{ikey}")
    assert stored is not None
    assert stored["delivered_via"] == "notification_service"
