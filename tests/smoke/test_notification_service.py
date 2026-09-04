"""Smoke-тесты ядра Доставщика (WP-418 Ф3, core.notification_service).

see DP.SC.177, DP.ROLE.075.

Без живой БД: лёгкий фейк asyncpg-conn/pool. Проверяем наблюдаемое поведение —
потолок класса подавляет, дедуп подавляет, очередь принимает, дренаж доставляет —
а не «импортировалось и не упало».
"""
import pytest

import core.notification_service as ns


# ─────────────────────────── фейк asyncpg ───────────────────────────

class FakeConn:
    def __init__(self, cap_count=0, duplicate=False, drain_rows=None):
        self._cap_count = cap_count
        self._duplicate = duplicate
        self._drain_rows = drain_rows or []
        self._next_id = 100
        self.updates = []  # id строк, по которым прошёл UPDATE (дренаж)
        self.update_sqls = []  # SQL UPDATE'ов — различение sent / suppressed (Ф4)
        self.inserts = []  # args INSERT'ов — проверка journal_key/journal_type (Ф4)
        self.receipt_updates = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return None
            async def __aexit__(self_, *a):
                return False
        return _Tx()

    async def execute(self, sql, *args):
        if sql.strip().upper().startswith("UPDATE"):
            if "development.nudge_receipt" in sql:
                self.receipt_updates.append(args[0] if args else None)
            else:
                self.updates.append(args[0] if args else None)
                self.update_sqls.append(sql)
        return "OK"

    async def fetchval(self, sql, *args):
        return self._cap_count  # COUNT для потолка класса

    async def fetchrow(self, sql, *args):
        if sql.strip().startswith("SELECT 1"):  # _is_duplicate
            return {"x": 1} if self._duplicate else None
        self._next_id += 1  # INSERT ... RETURNING id
        self.inserts.append(args)
        return {"id": self._next_id}

    async def fetch(self, sql, *args):
        return self._drain_rows


class FakePool:
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


def _patch_pool(monkeypatch, conn):
    pool = FakePool(conn)

    async def _get_pool():
        return pool
    monkeypatch.setattr(ns, "get_pool", _get_pool)


# ─────────────────────────── класс-модель ───────────────────────────

def test_all_five_classes_present():
    assert set(ns.CLASS_POLICY) == {
        ns.CLASS_CRITICAL, ns.CLASS_MUST_DELIVER, ns.CLASS_TRANSACTIONAL,
        ns.CLASS_CAPPED, ns.CLASS_OPS_ALERT,
    }


def test_priority_strictly_orders_user_classes():
    # critical < must-deliver < transactional < capped (1 = высший)
    p = ns.CLASS_POLICY
    assert (p[ns.CLASS_CRITICAL].priority
            < p[ns.CLASS_MUST_DELIVER].priority
            < p[ns.CLASS_TRANSACTIONAL].priority
            < p[ns.CLASS_CAPPED].priority)


def test_only_capped_has_daily_limit():
    # Гарантия РП406: лимит только у capped, must-deliver/critical вне потолка.
    assert ns.CLASS_POLICY[ns.CLASS_CAPPED].daily_cap == 2
    assert ns.CLASS_POLICY[ns.CLASS_CRITICAL].daily_cap is None
    assert ns.CLASS_POLICY[ns.CLASS_MUST_DELIVER].daily_cap is None


# ─────────────────────────── валидация (без БД) ───────────────────────────

@pytest.mark.asyncio
async def test_enqueue_rejects_unknown_class():
    with pytest.raises(ValueError):
        await ns.enqueue(123, "marketing-blast", {"text": "hi"})


@pytest.mark.asyncio
async def test_enqueue_rejects_empty_text():
    with pytest.raises(ValueError):
        await ns.enqueue(123, ns.CLASS_CAPPED, {"text": ""})


# ─────────────────────────── поведение enqueue ───────────────────────────

@pytest.mark.asyncio
async def test_under_cap_is_queued(monkeypatch):
    _patch_pool(monkeypatch, FakeConn(cap_count=0))
    res = await ns.enqueue(123, ns.CLASS_CAPPED, {"text": "nudge"})
    assert res["status"] == "queued"
    assert res["notification_id"] is not None


@pytest.mark.asyncio
async def test_capped_over_limit_is_suppressed(monkeypatch):
    # уже 2 за день → третий capped подавлен (сердце гарантии «≤2/день»)
    _patch_pool(monkeypatch, FakeConn(cap_count=2))
    res = await ns.enqueue(123, ns.CLASS_CAPPED, {"text": "nudge #3"})
    assert res["status"] == "suppressed"
    assert res["reason"] == "cap-exceeded"


@pytest.mark.asyncio
async def test_critical_ignores_cap(monkeypatch):
    # critical без лимита: даже при 99 за день — проходит
    _patch_pool(monkeypatch, FakeConn(cap_count=99))
    res = await ns.enqueue(123, ns.CLASS_CRITICAL, {"text": "подозрительный вход"})
    assert res["status"] == "queued"


@pytest.mark.asyncio
async def test_duplicate_dedup_key_suppressed(monkeypatch):
    _patch_pool(monkeypatch, FakeConn(duplicate=True))
    res = await ns.enqueue(123, ns.CLASS_CAPPED, {"text": "dup"}, dedup_key="k1")
    assert res["status"] == "suppressed"
    assert res["reason"] == "duplicate"


# ─────────────────────────── поведение drain ───────────────────────────

@pytest.mark.asyncio
async def test_drain_delivers_and_marks_sent(monkeypatch):
    row = {"id": 7, "chat_id": 555, "notification_class": ns.CLASS_MUST_DELIVER,
           "payload": '{"text": "урок дня"}', "priority": 2,
           "journal_key": None, "journal_type": None}
    conn = FakeConn(drain_rows=[row])
    _patch_pool(monkeypatch, conn)

    journaled = []

    async def _capture_journal(**kwargs):
        journaled.append(kwargs)
        return True
    monkeypatch.setattr(ns, "try_insert_notification", _capture_journal)

    delivered = []

    async def deliver(chat_id, content_spec):
        delivered.append((chat_id, content_spec["text"]))

    stats = await ns.drain(deliver)

    assert delivered == [(555, "урок дня")]          # реально доставлено
    assert stats["delivered"] == 1
    assert conn.updates == [7]                        # статус помечен sent (log-before-send)
    assert conn.receipt_updates == [7]                # receipt подтверждён только после deliver
    # без journal_* — технический fallback (canary/ops-alert)
    assert journaled[0]["idempotency_key"] == "delivery:7"
    assert journaled[0]["notification_type"] == ns.CLASS_MUST_DELIVER


@pytest.mark.asyncio
async def test_transport_failure_leaves_once_receipt_reserved(monkeypatch):
    row = {
        "id": 8,
        "chat_id": 555,
        "notification_class": ns.CLASS_CAPPED,
        "payload": '{"text": "milestone"}',
        "priority": 4,
        "journal_key": "nudge:555:2026-08-06:nudge_sessions_10",
        "journal_type": "nudge",
    }
    conn = FakeConn(drain_rows=[row])
    _patch_pool(monkeypatch, conn)

    async def _journal_new(**_kwargs):
        return True

    async def _ambiguous_failure(_chat_id, _content_spec):
        raise TimeoutError("transport outcome unknown")

    monkeypatch.setattr(ns, "try_insert_notification", _journal_new)

    stats = await ns.drain(_ambiguous_failure)

    assert stats == {"delivered": 0, "failed": 1}
    assert conn.receipt_updates == []


@pytest.mark.asyncio
async def test_drain_journals_semantic_key_and_type(monkeypatch):
    # Ф4: контракт readers (was_nudge_sent_recently ищет
    # external_id LIKE 'notification-nudge:{chat}:%:{key}' и type='nudge') —
    # drain обязан журналировать СЕМАНТИЧЕСКИМ ключом/типом отправителя, не классом.
    row = {"id": 9, "chat_id": 555, "notification_class": ns.CLASS_CAPPED,
           "payload": '{"text": "nudge"}', "priority": 4,
           "journal_key": "nudge:555:2026-06-12:slot_missing_3d",
           "journal_type": "nudge"}
    conn = FakeConn(drain_rows=[row])
    _patch_pool(monkeypatch, conn)

    journaled = []

    async def _capture_journal(**kwargs):
        journaled.append(kwargs)
        return True
    monkeypatch.setattr(ns, "try_insert_notification", _capture_journal)

    async def deliver(chat_id, content_spec):
        pass

    await ns.drain(deliver)

    assert journaled[0]["idempotency_key"] == "nudge:555:2026-06-12:slot_missing_3d"
    assert journaled[0]["notification_type"] == "nudge"   # не 'capped'


@pytest.mark.asyncio
async def test_drain_suppresses_journal_duplicate(monkeypatch):
    # Журнал помнит ключ (отправлено старым кодом до деплоя / параллельным
    # инстансом) → дубль НЕ доставляется, строка помечается suppressed.
    row = {"id": 11, "chat_id": 555, "notification_class": ns.CLASS_CAPPED,
           "payload": '{"text": "nudge"}', "priority": 4,
           "journal_key": "marathon_practice_nudge:555:3:30m",
           "journal_type": "marathon_practice_nudge"}
    conn = FakeConn(drain_rows=[row])
    _patch_pool(monkeypatch, conn)

    async def _journal_remembers(**kwargs):
        return False  # UNIQUE(source, external_id) — ключ уже есть
    monkeypatch.setattr(ns, "try_insert_notification", _journal_remembers)

    delivered = []

    async def deliver(chat_id, content_spec):
        delivered.append(chat_id)

    stats = await ns.drain(deliver)

    assert delivered == []                            # дубль не ушёл пользователю
    assert stats["delivered"] == 0
    assert conn.updates == [11]
    assert "suppressed" in conn.update_sqls[0]        # помечен suppressed, не sent


@pytest.mark.asyncio
async def test_enqueue_rejects_action_without_label():
    with pytest.raises(ValueError):
        await ns.enqueue(123, ns.CLASS_CAPPED,
                         {"text": "hi", "actions": [{"action": "go"}]})


@pytest.mark.asyncio
async def test_enqueue_rejects_oversize_callback_data():
    with pytest.raises(ValueError):
        await ns.enqueue(123, ns.CLASS_CAPPED,
                         {"text": "hi",
                          "actions": [{"label": "ок", "action": "x" * 65}]})


@pytest.mark.asyncio
async def test_enqueue_persists_journal_fields(monkeypatch):
    conn = FakeConn(cap_count=0)
    _patch_pool(monkeypatch, conn)
    res = await ns.enqueue(
        123, ns.CLASS_CAPPED, {"text": "nudge", "format": "markdown"},
        dedup_key="k1", journal_key="nudge:123:2026-06-12:k1", journal_type="nudge",
    )
    assert res["status"] == "queued"
    insert_args = conn.inserts[-1]
    assert "nudge:123:2026-06-12:k1" in insert_args   # journal_key дошёл до очереди
    assert "nudge" in insert_args                      # journal_type дошёл до очереди
