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
    def __init__(self, cap_count=0, duplicate=False, drain_rows=None, receipt_fails=False,
                 bot_blocked=False):
        self._cap_count = cap_count
        self._duplicate = duplicate
        self._drain_rows = drain_rows or []
        self._receipt_fails = receipt_fails
        self._bot_blocked = bot_blocked
        self._next_id = 100
        self.status_by_id = {}  # queue row id -> last status written by the drain
        self.reason_by_id = {}
        self.retry_delays = {}  # queue row id -> retry delay in minutes
        self.inserts = []  # args INSERT'ов — проверка journal_key/journal_type (Ф4)
        self.receipt_updates = []
        self.stale_sweeps = 0

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return None
            async def __aexit__(self_, *a):
                return False
        return _Tx()

    async def execute(self, sql, *args):
        if not sql.strip().upper().startswith("UPDATE"):
            return "OK"
        if "development.nudge_receipt" in sql:
            if self._receipt_fails:
                raise Exception('UndefinedTableError: relation "development.nudge_receipt" does not exist')
            self.receipt_updates.append(args[0])
        elif "stale-sending" in sql:
            self.stale_sweeps += 1
        elif "id = ANY" in sql:
            for queue_id in args[0]:
                self.status_by_id[queue_id] = "sending"
        elif "status = 'sent'" in sql:
            self.status_by_id[args[0]] = "sent"
        elif "status = 'queued'" in sql:
            self.status_by_id[args[0]] = "queued"
            self.reason_by_id[args[0]] = args[1]
            self.retry_delays[args[0]] = args[2]
        else:  # _set_status(id, status, reason)
            self.status_by_id[args[0]] = args[1]
            self.reason_by_id[args[0]] = args[2]
        return "OK"

    async def fetchval(self, sql, *args):
        if "bot_blocked" in sql:
            return self._bot_blocked
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
# РП-568 Ф16 шаг 1а: каждая строка получает исход из закрытого списка, журнал
# пишется только после доставки, повтор — только для временных ошибок.

def _row(queue_id=7, klass=ns.CLASS_MUST_DELIVER, journal_key=None, journal_type=None,
         attempts=0, text="урок дня"):
    return {"id": queue_id, "chat_id": 555, "notification_class": klass,
            "payload": '{"text": "%s"}' % text, "priority": 2,
            "journal_key": journal_key, "journal_type": journal_type,
            "attempts": attempts}


def _patch_journal(monkeypatch, already_sent=False, check_fails=False):
    journaled = []

    async def _was_sent(_key):
        if check_fails:
            raise ConnectionError("learning pool down")
        return already_sent

    async def _capture(**kwargs):
        journaled.append(kwargs)
        return True

    monkeypatch.setattr(ns, "was_notification_sent", _was_sent)
    monkeypatch.setattr(ns, "try_insert_notification", _capture)
    return journaled


@pytest.mark.asyncio
async def test_drain_delivers_marks_sent_and_journals_after(monkeypatch):
    conn = FakeConn(drain_rows=[_row()])
    _patch_pool(monkeypatch, conn)
    journaled = _patch_journal(monkeypatch)
    delivered = []

    async def deliver(chat_id, content_spec):
        # The row is already claimed, and nothing is journaled yet.
        assert conn.status_by_id[7] == "sending"
        assert journaled == []
        delivered.append((chat_id, content_spec["text"]))

    stats = await ns.drain(deliver)

    assert delivered == [(555, "урок дня")]
    assert stats == {"delivered": 1, "failed": 0}
    assert conn.status_by_id[7] == "sent"
    assert conn.receipt_updates == [7]
    assert conn.stale_sweeps == 1
    # без journal_* — технический fallback (canary/ops-alert)
    assert journaled[0]["idempotency_key"] == "delivery:7"
    assert journaled[0]["notification_type"] == ns.CLASS_MUST_DELIVER


@pytest.mark.asyncio
async def test_drain_journals_semantic_key_and_type(monkeypatch):
    # Контракт readers (was_nudge_sent_recently) — ключ и тип отправителя, не класс.
    row = _row(queue_id=9, klass=ns.CLASS_CAPPED,
               journal_key="nudge:555:2026-06-12:slot_missing_3d", journal_type="nudge")
    conn = FakeConn(drain_rows=[row])
    _patch_pool(monkeypatch, conn)
    journaled = _patch_journal(monkeypatch)

    async def deliver(_chat_id, _content_spec):
        return None

    await ns.drain(deliver)

    assert journaled[0]["idempotency_key"] == "nudge:555:2026-06-12:slot_missing_3d"
    assert journaled[0]["notification_type"] == "nudge"


@pytest.mark.asyncio
async def test_drain_skips_row_already_in_journal(monkeypatch):
    # Журнал помнит ключ (старый код до деплоя / второй экземпляр) → дубль не уходит.
    row = _row(queue_id=11, klass=ns.CLASS_CAPPED,
               journal_key="marathon_practice_nudge:555:3:30m",
               journal_type="marathon_practice_nudge")
    conn = FakeConn(drain_rows=[row])
    _patch_pool(monkeypatch, conn)
    journaled = _patch_journal(monkeypatch, already_sent=True)
    delivered = []

    async def deliver(chat_id, _content_spec):
        delivered.append(chat_id)

    stats = await ns.drain(deliver)

    assert delivered == []
    assert stats == {"delivered": 0, "failed": 0}
    assert conn.status_by_id[11] == "suppressed"
    assert conn.reason_by_id[11] == "journal-dup"
    assert journaled == []


@pytest.mark.asyncio
async def test_drain_blocked_user_is_final_and_not_journaled(monkeypatch):
    conn = FakeConn(drain_rows=[_row()])
    _patch_pool(monkeypatch, conn)
    journaled = _patch_journal(monkeypatch)

    async def deliver(_chat_id, _content_spec):
        raise ns.DeliveryBlocked("Forbidden: bot was blocked by the user")

    stats = await ns.drain(deliver)

    assert stats == {"delivered": 0, "failed": 1}
    assert conn.status_by_id[7] == "blocked"
    assert journaled == []
    assert conn.receipt_updates == []


@pytest.mark.asyncio
async def test_drain_permanent_failure_is_not_retried(monkeypatch):
    conn = FakeConn(drain_rows=[_row()])
    _patch_pool(monkeypatch, conn)
    journaled = _patch_journal(monkeypatch)

    async def deliver(_chat_id, _content_spec):
        raise ns.DeliveryPermanent("Bad Request: message is too long")

    await ns.drain(deliver)

    assert conn.status_by_id[7] == "failed_permanent"
    assert conn.retry_delays == {}
    assert journaled == []


@pytest.mark.asyncio
async def test_drain_transient_failure_requeues_with_backoff(monkeypatch):
    conn = FakeConn(drain_rows=[_row(attempts=0)])
    _patch_pool(monkeypatch, conn)
    journaled = _patch_journal(monkeypatch)

    async def deliver(_chat_id, _content_spec):
        raise ns.DeliveryTransient("Too Many Requests: retry after 5")

    stats = await ns.drain(deliver)

    assert stats == {"delivered": 0, "failed": 1}
    assert conn.status_by_id[7] == "queued"
    assert conn.retry_delays[7] == 2   # first attempt failed → 2^1 minutes
    assert journaled == []


@pytest.mark.asyncio
async def test_drain_transient_failure_gives_up_after_max_attempts(monkeypatch):
    conn = FakeConn(drain_rows=[_row(attempts=ns.MAX_ATTEMPTS - 1)])
    _patch_pool(monkeypatch, conn)
    _patch_journal(monkeypatch)

    async def deliver(_chat_id, _content_spec):
        raise ns.DeliveryTransient("Internal Server Error")

    await ns.drain(deliver)

    assert conn.status_by_id[7] == "failed_permanent"
    assert conn.reason_by_id[7].startswith("retries-exhausted")
    assert conn.retry_delays == {}


@pytest.mark.asyncio
async def test_drain_unknown_outcome_is_not_retried_or_journaled(monkeypatch):
    # Таймаут: запрос мог дойти до Telegram — повтор дал бы дубль.
    row = _row(queue_id=8, klass=ns.CLASS_CAPPED,
               journal_key="nudge:555:2026-08-06:nudge_sessions_10", journal_type="nudge")
    conn = FakeConn(drain_rows=[row])
    _patch_pool(monkeypatch, conn)
    journaled = _patch_journal(monkeypatch)

    async def deliver(_chat_id, _content_spec):
        raise TimeoutError("transport outcome unknown")

    stats = await ns.drain(deliver)

    assert stats == {"delivered": 0, "failed": 1}
    assert conn.status_by_id[8] == "unknown"
    assert conn.retry_delays == {}
    assert journaled == []
    assert conn.receipt_updates == []


@pytest.mark.asyncio
async def test_drain_journal_check_failure_requeues_without_sending(monkeypatch):
    conn = FakeConn(drain_rows=[_row()])
    _patch_pool(monkeypatch, conn)
    _patch_journal(monkeypatch, check_fails=True)
    delivered = []

    async def deliver(chat_id, _content_spec):
        delivered.append(chat_id)

    await ns.drain(deliver)

    assert delivered == []
    assert conn.status_by_id[7] == "queued"


@pytest.mark.asyncio
async def test_drain_survives_receipt_settlement_failure(monkeypatch):
    # Сломанный settlement (таблица nudge_receipt ещё не создана) не отменяет
    # уже случившуюся доставку.
    conn = FakeConn(drain_rows=[_row(queue_id=9, klass=ns.CLASS_CAPPED)], receipt_fails=True)
    _patch_pool(monkeypatch, conn)
    _patch_journal(monkeypatch)
    delivered = []

    async def deliver(chat_id, _content_spec):
        delivered.append(chat_id)

    stats = await ns.drain(deliver)

    assert delivered == [555]
    assert stats["delivered"] == 1
    assert conn.status_by_id[9] == "sent"
    assert conn.receipt_updates == []


@pytest.mark.asyncio
async def test_enqueue_suppresses_optional_nudge_to_user_who_blocked_bot(monkeypatch):
    conn = FakeConn(bot_blocked=True)
    _patch_pool(monkeypatch, conn)
    res = await ns.enqueue(123, ns.CLASS_CAPPED, {"text": "напоминание"})
    assert res["status"] == "suppressed"
    assert res["reason"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("klass", [ns.CLASS_CRITICAL, ns.CLASS_MUST_DELIVER,
                                   ns.CLASS_TRANSACTIONAL, ns.CLASS_OPS_ALERT])
async def test_enqueue_non_optional_classes_ignore_stale_block_flag(monkeypatch, klass):
    # bot_blocked clears only when the user writes; a payment or access message to
    # someone who already unblocked must still be tried (the transport marks real blocks).
    conn = FakeConn(bot_blocked=True)
    _patch_pool(monkeypatch, conn)
    res = await ns.enqueue(123, klass, {"text": "оплата прошла"})
    assert res["status"] == "queued"


class _FailingStatusConn(FakeConn):
    """FakeConn whose UPDATE containing a given SQL fragment raises (DB error mid-batch)."""

    def __init__(self, failing_sql, **kwargs):
        super().__init__(**kwargs)
        self._failing_sql = failing_sql
        self.released = []

    async def execute(self, sql, *args):
        if "SET status = 'queued', locked_at = NULL" in sql and "ANY" in sql:
            self.released.extend(args[0])
            return "OK"
        if self._failing_sql in sql:
            raise ConnectionError("main pool dropped")
        return await super().execute(sql, *args)


@pytest.mark.asyncio
async def test_db_error_after_send_never_requeues(monkeypatch):
    # Telegram accepted the message, then the 'sent' UPDATE failed: the row must
    # stay out of the queue (no duplicate) and the rest of the batch still runs.
    rows = [_row(queue_id=1), _row(queue_id=2)]
    conn = _FailingStatusConn("SET status = 'sent'", drain_rows=rows)
    _patch_pool(monkeypatch, conn)
    _patch_journal(monkeypatch)
    delivered = []

    async def deliver(chat_id, _content_spec):
        delivered.append(chat_id)

    await ns.drain(deliver)

    assert delivered == [555, 555]          # second row was not stranded
    assert conn.released == []              # nothing went back to the queue


@pytest.mark.asyncio
async def test_db_error_before_send_requeues_the_row(monkeypatch):
    # The journal-dup status write fails before any transport call → back to 'queued'.
    rows = [_row(queue_id=1, journal_key="k1")]
    conn = _FailingStatusConn("SET status = $2", drain_rows=rows)
    _patch_pool(monkeypatch, conn)
    _patch_journal(monkeypatch, already_sent=True)
    delivered = []

    async def deliver(chat_id, _content_spec):
        delivered.append(chat_id)

    await ns.drain(deliver)

    assert delivered == []
    assert conn.released == [1]


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
