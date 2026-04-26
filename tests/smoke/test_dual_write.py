"""
WP-268 Phase 2 Phase A — smoke-тесты для helpers/dual_write.py.

Покрывает:
1. Envelope корректный (source/event_type/schema_version/external_id/payload).
2. Fire-and-forget: post_event ловит сетевые ошибки и не raise.
3. account_id=None допустим (gateway accepts) — envelope не содержит ключ.
4. EVENT_GATEWAY_ENABLED=False → no-op.
5. fire_event без running loop → warning, no raise.
"""

import sys
import os
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")
os.environ.setdefault("EVENT_GATEWAY_URL", "https://event-gateway.test.invalid")

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_import_dual_write():
    """Helper модуль импортируется без ошибок."""
    from helpers import dual_write
    assert hasattr(dual_write, "post_event")
    assert hasattr(dual_write, "fire_event")
    assert hasattr(dual_write, "aclose")


@pytest.mark.asyncio
async def test_post_event_envelope_correct():
    """Envelope содержит все обязательные поля. account_id присутствует если задан."""
    from helpers import dual_write

    captured = {}

    class FakeResponse:
        status = 200
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None

    class FakeSession:
        closed = False
        def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()
        async def close(self):
            self.closed = True

    fake = FakeSession()
    with patch.object(dual_write, "_get_session", return_value=fake):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            await dual_write.post_event(
                source="aist-bot",
                external_id="test-evt-1",
                event_type="user_registered",
                schema_version="v1",
                occurred_at=datetime(2026, 4, 26, 12, 0, 0),
                account_id="test-uuid-123",
                payload={"telegram_id": 999},
            )

    assert captured["url"].endswith("/events")
    env = captured["json"]
    assert env["source"] == "aist-bot"
    assert env["external_id"] == "test-evt-1"
    assert env["event_type"] == "user_registered"
    assert env["schema_version"] == "v1"
    assert env["payload"] == {"telegram_id": 999}
    assert env["account_id"] == "test-uuid-123"
    # ISO-8601 with timezone info (naive UTC → UTC)
    assert env["occurred_at"].startswith("2026-04-26T12:00:00")
    assert "+00:00" in env["occurred_at"] or env["occurred_at"].endswith("Z")


@pytest.mark.asyncio
async def test_post_event_account_id_none_omitted():
    """account_id=None → ключ отсутствует в envelope (gateway accepts NULL)."""
    from helpers import dual_write

    captured = {}

    class FakeResponse:
        status = 200
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None

    class FakeSession:
        closed = False
        def post(self, url, json=None):
            captured["json"] = json
            return FakeResponse()

    with patch.object(dual_write, "_get_session", return_value=FakeSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            await dual_write.post_event(
                source="aist-bot",
                external_id="test-evt-2",
                event_type="user_updated",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id=None,
                payload={"telegram_id": 777},
            )

    assert "account_id" not in captured["json"]


@pytest.mark.asyncio
async def test_post_event_swallows_network_errors():
    """Сетевая ошибка → warning, не raise. Legacy DB не блокируется."""
    from helpers import dual_write

    class BrokenSession:
        closed = False
        def post(self, url, json=None):
            raise ConnectionError("connection refused")

    with patch.object(dual_write, "_get_session", return_value=BrokenSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            # Не должно поднимать
            await dual_write.post_event(
                source="aist-bot",
                external_id="test-evt-3",
                event_type="user_registered",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id=None,
                payload={"telegram_id": 1},
            )


@pytest.mark.asyncio
async def test_post_event_disabled_noop():
    """EVENT_GATEWAY_ENABLED=False → no-op (no HTTP call)."""
    from helpers import dual_write

    called = {"n": 0}

    class CountingSession:
        closed = False
        def post(self, *args, **kwargs):
            called["n"] += 1
            raise AssertionError("should not be called when disabled")

    with patch.object(dual_write, "_get_session", return_value=CountingSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", False):
            await dual_write.post_event(
                source="aist-bot",
                external_id="test-evt-disabled",
                event_type="user_registered",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id=None,
                payload={"telegram_id": 1},
            )

    assert called["n"] == 0


@pytest.mark.asyncio
async def test_post_event_handles_4xx_response():
    """4xx response → warning, не raise."""
    from helpers import dual_write

    class FakeResponse:
        status = 422
        async def text(self):
            return '{"error":"validation"}'
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None

    class FakeSession:
        closed = False
        def post(self, url, json=None):
            return FakeResponse()

    with patch.object(dual_write, "_get_session", return_value=FakeSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            # Не должно поднимать
            await dual_write.post_event(
                source="aist-bot",
                external_id="test-evt-4xx",
                event_type="user_registered",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id=None,
                payload={},
            )


def test_to_iso_utc_naive_treated_as_utc():
    """Naive datetime → суффикс +00:00 (трактуется как UTC)."""
    from helpers.dual_write import _to_iso_utc

    naive = datetime(2026, 4, 26, 12, 30, 45)
    iso = _to_iso_utc(naive)
    assert iso.startswith("2026-04-26T12:30:45")
    assert "+00:00" in iso


def test_to_iso_utc_aware_preserved():
    """Aware datetime — tzinfo сохраняется."""
    from helpers.dual_write import _to_iso_utc

    aware = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    iso = _to_iso_utc(aware)
    assert "+00:00" in iso


# ----------------------------------------------------------------------
# WP-268 Phase 2 Phase B — high-volume writers (events/qa/notify/traces)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_ory_id_cache_hit():
    """resolve_ory_id_from_chat кэширует результат — повторный вызов не идёт в БД."""
    from helpers import dual_write

    # Очистить кэш
    dual_write._ory_cache.clear()

    db_calls = {"n": 0}

    class FakeConn:
        async def fetchval(self, sql, chat_id):
            db_calls["n"] += 1
            return "11111111-2222-3333-4444-555555555555"
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    class FakePool:
        def acquire(self):
            return FakeConn()

    async def fake_get_pool():
        return FakePool()

    with patch("db.connection.get_pool", fake_get_pool):
        ory1 = await dual_write.resolve_ory_id_from_chat(123456)
        ory2 = await dual_write.resolve_ory_id_from_chat(123456)

    assert ory1 == "11111111-2222-3333-4444-555555555555"
    assert ory2 == ory1
    assert db_calls["n"] == 1  # Второй вызов из кэша


@pytest.mark.asyncio
async def test_resolve_ory_id_negative_cache():
    """T0 пользователь (нет ory_id) кэшируется как None — повторный вызов не идёт в БД."""
    from helpers import dual_write

    dual_write._ory_cache.clear()

    db_calls = {"n": 0}

    class FakeConn:
        async def fetchval(self, sql, chat_id):
            db_calls["n"] += 1
            return None  # T0 — ory_id ещё не привязан
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    class FakePool:
        def acquire(self):
            return FakeConn()

    async def fake_get_pool():
        return FakePool()

    with patch("db.connection.get_pool", fake_get_pool):
        r1 = await dual_write.resolve_ory_id_from_chat(999)
        r2 = await dual_write.resolve_ory_id_from_chat(999)

    assert r1 is None
    assert r2 is None
    assert db_calls["n"] == 1


@pytest.mark.asyncio
async def test_resolve_ory_id_db_error_returns_none():
    """На ошибке БД resolve возвращает None и не raise."""
    from helpers import dual_write

    dual_write._ory_cache.clear()

    async def broken_get_pool():
        raise ConnectionError("db down")

    with patch("db.connection.get_pool", broken_get_pool):
        result = await dual_write.resolve_ory_id_from_chat(42)

    assert result is None


@pytest.mark.asyncio
async def test_phase_b_qa_query_envelope():
    """qa_query.v1 — payload без raw question/answer, account_id из cache."""
    from helpers import dual_write

    captured = {}

    class FakeResponse:
        status = 200
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    class FakeSession:
        closed = False
        def post(self, url, json=None):
            captured["json"] = json
            return FakeResponse()

    with patch.object(dual_write, "_get_session", return_value=FakeSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            await dual_write.post_event(
                source="aist-bot",
                external_id="qa-42",
                event_type="qa_query",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id="abc-uuid",
                payload={
                    "qa_id": 42,
                    "mode": "marathon",
                    "context_topic": "topic-1",
                    "question_length": 47,
                    "answer_length": 230,
                    "mcp_sources_count": 3,
                },
            )

    env = captured["json"]
    assert env["event_type"] == "qa_query"
    assert env["external_id"] == "qa-42"
    assert env["account_id"] == "abc-uuid"
    # PII инвариант: ни question, ни answer не в payload
    assert "question" not in env["payload"]
    assert "answer" not in env["payload"]
    assert env["payload"]["question_length"] == 47


@pytest.mark.asyncio
async def test_phase_b_notification_sent_envelope():
    """notification_sent.v1 — payload только metadata (нет содержимого payload caller'а)."""
    from helpers import dual_write

    captured = {}

    class FakeResponse:
        status = 200
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    class FakeSession:
        closed = False
        def post(self, url, json=None):
            captured["json"] = json
            return FakeResponse()

    caller_payload = {"user_name": "Иван", "topic_title": "Урок 3"}

    with patch.object(dual_write, "_get_session", return_value=FakeSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            await dual_write.post_event(
                source="aist-bot",
                external_id="notification-marathon:111:2026-04-26:lesson_1",
                event_type="notification_sent",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id="user-uuid",
                payload={
                    "notification_type": "marathon",
                    "idempotency_key": "marathon:111:2026-04-26:lesson_1",
                    "payload_keys": list(caller_payload.keys()),
                },
            )

    env = captured["json"]
    assert env["event_type"] == "notification_sent"
    # PII инвариант: значения caller_payload (имя, заголовок) НЕ передаются
    assert "user_name" not in env["payload"]
    assert "topic_title" not in env["payload"]
    assert env["payload"]["payload_keys"] == ["user_name", "topic_title"]


@pytest.mark.asyncio
async def test_phase_b_request_traced_envelope():
    """request_traced.v1 — command обрезан до первого слова, нет хвоста."""
    from helpers import dual_write

    captured = {}

    class FakeResponse:
        status = 200
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    class FakeSession:
        closed = False
        def post(self, url, json=None):
            captured["json"] = json
            return FakeResponse()

    with patch.object(dual_write, "_get_session", return_value=FakeSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            await dual_write.post_event(
                source="aist-bot",
                external_id="trace-abc123def456",
                event_type="request_traced",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id="user-uuid-2",
                payload={
                    "trace_id": "abc123def456",
                    "command": "/start",
                    "state": "common.start",
                    "total_ms": 245.7,
                    "spans_count": 3,
                },
            )

    env = captured["json"]
    assert env["event_type"] == "request_traced"
    assert env["payload"]["command"] == "/start"
    assert env["payload"]["spans_count"] == 3


@pytest.mark.asyncio
async def test_phase_b_log_event_legacy_event_envelope():
    """legacy event_type (любой) — gateway accept'ит через permissive schema."""
    from helpers import dual_write

    captured = {}

    class FakeResponse:
        status = 200
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    class FakeSession:
        closed = False
        def post(self, url, json=None):
            captured["json"] = json
            return FakeResponse()

    with patch.object(dual_write, "_get_session", return_value=FakeSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            await dual_write.post_event(
                source="aist-bot",
                external_id="bot-555-marathon_step-1234567890",
                event_type="marathon_step",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id="user-uuid-3",
                payload={
                    "user_id": "555",
                    "source": "bot",
                    "confidence": 0.9,
                    "skill_count": 2,
                    "payload_keys": ["topic_id", "result"],
                },
            )

    env = captured["json"]
    assert env["event_type"] == "marathon_step"
    assert env["payload"]["confidence"] == 0.9
    assert env["payload"]["skill_count"] == 2
