"""Smoke-тесты WP-117 Ф-decouple deliverables (core.nudge_delivery).

Без живой БД: лёгкий фейк asyncpg-conn/pool (тот же паттерн, что
test_notification_service.py). Проверяем наблюдаемое поведение — cooldown
подавляет, потолок capped подавляет, exclusive преемптит батч, unknown_type
отклоняется — а не «импортировалось и не упало».
"""
import pytest

import core.nudge_delivery as nd


# ─────────────────────────── фейк asyncpg ───────────────────────────

class FakeConn:
    def __init__(self, cap_count=0, duplicate=False):
        self._cap_count = cap_count
        self._duplicate = duplicate
        self._next_id = 100
        self.inserts = []  # (chat_id, notification_class, payload, priority, dedup_key, journal_key, journal_type)

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *a):
                return False
        return _Tx()

    async def execute(self, sql, *args):
        return "OK"

    async def fetchval(self, sql, *args):
        return self._cap_count

    async def fetchrow(self, sql, *args):
        if sql.strip().startswith("SELECT 1"):
            return {"x": 1} if self._duplicate else None
        self._next_id += 1
        self.inserts.append(args)
        return {"id": self._next_id}


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
    monkeypatch.setattr(nd, "get_pool", _get_pool)


def _register(monkeypatch, **types):
    monkeypatch.setattr(nd, "NUDGE_TYPE_CONFIG", types)


# ─────────────────────────── unknown type ───────────────────────────

@pytest.mark.asyncio
async def test_unknown_nudge_type_rejected(monkeypatch):
    _register(monkeypatch)  # пусто — ничего не зарегистрировано
    candidate = nd.NudgeCandidate(user_id=1, nudge_type="ghost", payload={"text": "hi"},
                                   dedup_key="k1", priority=4)
    results = await nd.select_and_enqueue([candidate])
    assert results == [nd.EnqueueResult(user_id=1, nudge_type="ghost", enqueued=False, reason="unknown_type")]


# ─────────────────────────── cooldown / cap ───────────────────────────

@pytest.mark.asyncio
async def test_under_cap_is_enqueued(monkeypatch):
    _register(monkeypatch, engagement=nd.NudgeTypeConfig(
        nudge_type="engagement", cooldown_days=1, class_cap=nd.ClassCap.CLASS_CAPPED))
    conn = FakeConn(cap_count=0, duplicate=False)
    _patch_pool(monkeypatch, conn)

    candidate = nd.NudgeCandidate(user_id=1, nudge_type="engagement", payload={"text": "hi"},
                                   dedup_key="k1", priority=4)
    results = await nd.select_and_enqueue([candidate])

    assert results == [nd.EnqueueResult(user_id=1, nudge_type="engagement", enqueued=True, reason=None)]
    assert conn.inserts[-1][1] == "capped"  # CLASS_CAPPED пишется в общий бакет 'capped'


@pytest.mark.asyncio
async def test_capped_over_daily_limit_suppressed(monkeypatch):
    # уже 2 sent/queued сегодня в бакете 'capped' — третий (любого nudge_type с CLASS_CAPPED) подавлен
    _register(monkeypatch, marathon=nd.NudgeTypeConfig(
        nudge_type="marathon", cooldown_days=1, class_cap=nd.ClassCap.CLASS_CAPPED))
    conn = FakeConn(cap_count=2, duplicate=False)
    _patch_pool(monkeypatch, conn)

    candidate = nd.NudgeCandidate(user_id=1, nudge_type="marathon", payload={"text": "hi"},
                                   dedup_key="k2", priority=4)
    results = await nd.select_and_enqueue([candidate])

    assert results == [nd.EnqueueResult(user_id=1, nudge_type="marathon", enqueued=False, reason="cap-exceeded")]
    assert conn.inserts == []


@pytest.mark.asyncio
async def test_cooldown_dedup_key_suppressed(monkeypatch):
    _register(monkeypatch, engagement=nd.NudgeTypeConfig(
        nudge_type="engagement", cooldown_days=7, class_cap=nd.ClassCap.CLASS_ANY))
    conn = FakeConn(cap_count=0, duplicate=True)
    _patch_pool(monkeypatch, conn)

    candidate = nd.NudgeCandidate(user_id=1, nudge_type="engagement", payload={"text": "hi"},
                                   dedup_key="dup-key", priority=4)
    results = await nd.select_and_enqueue([candidate])

    assert results == [nd.EnqueueResult(user_id=1, nudge_type="engagement", enqueued=False, reason="cooldown")]


@pytest.mark.asyncio
async def test_class_any_ignores_capped_bucket(monkeypatch):
    # CLASS_ANY не считается против capped-бакета — cap_count=99 капед-бакета не должен применяться
    _register(monkeypatch, transactional_like=nd.NudgeTypeConfig(
        nudge_type="transactional_like", cooldown_days=1, class_cap=nd.ClassCap.CLASS_ANY))
    conn = FakeConn(cap_count=99, duplicate=False)
    _patch_pool(monkeypatch, conn)

    candidate = nd.NudgeCandidate(user_id=1, nudge_type="transactional_like", payload={"text": "hi"},
                                   dedup_key="k3", priority=3)
    results = await nd.select_and_enqueue([candidate])

    assert results == [nd.EnqueueResult(user_id=1, nudge_type="transactional_like", enqueued=True, reason=None)]
    assert conn.inserts[-1][1] == "transactional_like"  # своя, не 'capped'


# ─────────────────────────── exclusive preemption ───────────────────────────

@pytest.mark.asyncio
async def test_exclusive_preempts_other_candidates_same_batch(monkeypatch):
    _register(
        monkeypatch,
        milestone=nd.NudgeTypeConfig(nudge_type="milestone", cooldown_days=1,
                                      class_cap=nd.ClassCap.CLASS_EXCLUSIVE),
        engagement=nd.NudgeTypeConfig(nudge_type="engagement", cooldown_days=1,
                                       class_cap=nd.ClassCap.CLASS_ANY),
    )
    conn = FakeConn(cap_count=0, duplicate=False)
    _patch_pool(monkeypatch, conn)

    candidates = [
        nd.NudgeCandidate(user_id=1, nudge_type="engagement", payload={"text": "e"}, dedup_key="e1", priority=4),
        nd.NudgeCandidate(user_id=1, nudge_type="milestone", payload={"text": "m"}, dedup_key="m1", priority=2),
    ]
    results = await nd.select_and_enqueue(candidates)

    by_type = {r.nudge_type: r for r in results}
    assert by_type["milestone"].enqueued is True
    assert by_type["engagement"].enqueued is False
    assert by_type["engagement"].reason == "exclusive-preempted"
    assert len(conn.inserts) == 1  # только milestone реально вставлен


# ─────────────────────────── batch grouping ───────────────────────────

@pytest.mark.asyncio
async def test_select_and_enqueue_groups_multiple_users(monkeypatch):
    _register(monkeypatch, engagement=nd.NudgeTypeConfig(
        nudge_type="engagement", cooldown_days=1, class_cap=nd.ClassCap.CLASS_ANY))
    conn = FakeConn(cap_count=0, duplicate=False)
    _patch_pool(monkeypatch, conn)

    candidates = [
        nd.NudgeCandidate(user_id=1, nudge_type="engagement", payload={"text": "a"}, dedup_key="a1", priority=4),
        nd.NudgeCandidate(user_id=2, nudge_type="engagement", payload={"text": "b"}, dedup_key="b1", priority=4),
    ]
    results = await nd.select_and_enqueue(candidates)

    assert {r.user_id for r in results} == {1, 2}
    assert all(r.enqueued for r in results)


@pytest.mark.asyncio
async def test_select_and_enqueue_empty_input():
    assert await nd.select_and_enqueue([]) == []


# ─────────────────────────── get_recent_nudges_batch ───────────────────────────

@pytest.mark.asyncio
async def test_get_recent_nudges_batch_filters_by_registered_types_and_reshapes(monkeypatch):
    _register(
        monkeypatch,
        engagement=nd.NudgeTypeConfig(nudge_type="engagement", cooldown_days=7,
                                       class_cap=nd.ClassCap.CLASS_ANY),
    )

    captured = {}

    async def _fake_fetch(user_ids, nudge_type_cooldowns):
        captured["user_ids"] = user_ids
        captured["cooldowns"] = nudge_type_cooldowns
        return {
            1: [{"nudge_type": "engagement", "sent_at": "2026-07-01T00:00:00Z", "status": "delivered"}],
            2: [],
        }
    monkeypatch.setattr(nd, "fetch_recent_nudges_by_type", _fake_fetch)

    result = await nd.get_recent_nudges_batch([1, 2], nudge_types=["engagement", "ghost-type"])

    # "ghost-type" не зарегистрирован в NUDGE_TYPE_CONFIG — не должен уйти в SQL-слой
    assert captured["cooldowns"] == {"engagement": 7}
    assert result[1] == [nd.NudgeRecord(user_id=1, nudge_type="engagement",
                                         sent_at="2026-07-01T00:00:00Z", status="delivered")]
    assert result[2] == []


@pytest.mark.asyncio
async def test_get_recent_nudges_batch_defaults_to_all_registered_types(monkeypatch):
    _register(
        monkeypatch,
        a=nd.NudgeTypeConfig(nudge_type="a", cooldown_days=1, class_cap=nd.ClassCap.CLASS_ANY),
        b=nd.NudgeTypeConfig(nudge_type="b", cooldown_days=2, class_cap=nd.ClassCap.CLASS_ANY),
    )
    captured = {}

    async def _fake_fetch(user_ids, nudge_type_cooldowns):
        captured["cooldowns"] = nudge_type_cooldowns
        return {}
    monkeypatch.setattr(nd, "fetch_recent_nudges_by_type", _fake_fetch)

    await nd.get_recent_nudges_batch([1])

    assert captured["cooldowns"] == {"a": 1, "b": 2}
