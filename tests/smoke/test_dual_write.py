"""
WP-268 Phase 2 Phase A — smoke-тесты для helpers/dual_write.py.

Покрывает:
1. Envelope корректный (source/event_type/schema_version/external_id/payload).
2. Fire-and-forget: post_event ловит сетевые ошибки и не raise.
3. account_id=None допустим (gateway accepts) — envelope не содержит ключ.
4. EVENT_GATEWAY_ENABLED=False → no-op.
5. fire_event без running loop → warning, no raise.
"""

import asyncio
import hashlib
import hmac
import json as jsonlib
import os
import sys
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
        def post(self, url, json=None, data=None, headers=None):
            captured["url"] = url
            captured["json"] = json if json is not None else jsonlib.loads(data)
            captured["data"] = data
            captured["headers"] = headers
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
async def test_post_event_hmac_covers_exact_body():
    """HMAC связывает источник, ключ, время и точные байты HTTP-тела."""
    from helpers import dual_write

    captured = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeSession:
        closed = False

        def post(self, url, json=None, data=None, headers=None):
            captured["data"] = data
            captured["headers"] = headers
            return FakeResponse()

    secret = "0123456789abcdef0123456789abcdef"
    with patch.object(dual_write, "_get_session", return_value=FakeSession()), \
         patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True), \
         patch.object(dual_write, "EVENT_GATEWAY_HMAC_KEY", secret), \
         patch.object(dual_write, "EVENT_GATEWAY_HMAC_KEY_ID", "current"), \
         patch.object(dual_write.time, "time", return_value=1785920400):
        await dual_write.post_event(
            source="aist-bot",
            external_id="signed-1",
            event_type="user_registered",
            schema_version="v1",
            occurred_at=datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc),
            account_id=None,
            payload={},
        )

    canonical = b"v1\naist-bot\ncurrent\n1785920400\n" + captured["data"]
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    assert captured["headers"]["X-IWE-Signature"] == f"sha256={expected}"


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
        def post(self, url, json=None, data=None, headers=None):
            captured["json"] = json if json is not None else jsonlib.loads(data)
            captured["headers"] = headers
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
        def post(self, url, json=None, data=None, headers=None):
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
        def post(self, url, json=None, data=None, headers=None):
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

    async def fake_get_persona_pool():
        return FakePool()

    with patch("db.connection.get_persona_pool", fake_get_persona_pool):
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

    async def fake_get_persona_pool():
        return FakePool()

    with patch("db.connection.get_persona_pool", fake_get_persona_pool):
        r1 = await dual_write.resolve_ory_id_from_chat(999)
        r2 = await dual_write.resolve_ory_id_from_chat(999)

    assert r1 is None
    assert r2 is None
    assert db_calls["n"] == 1


@pytest.mark.asyncio
async def test_resolve_ory_id_db_error_returns_none(caplog):
    """Ошибка resolve не раскрывает Telegram ID и текст исключения."""
    from helpers import dual_write

    dual_write._ory_cache.clear()
    telegram_id_marker = 987654321
    exception_marker = "DATABASE_SECRET_SENTINEL"

    async def broken_get_persona_pool():
        raise ConnectionError(exception_marker)

    with patch("db.connection.get_persona_pool", broken_get_persona_pool):
        result = await dual_write.resolve_ory_id_from_chat(telegram_id_marker)

    assert result is None
    assert "resolve_ory_id_from_chat failed: ConnectionError" in caplog.text
    assert str(telegram_id_marker) not in caplog.text
    assert exception_marker not in caplog.text


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
        def post(self, url, json=None, data=None, headers=None):
            captured["json"] = json if json is not None else jsonlib.loads(data)
            captured["headers"] = headers
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
        def post(self, url, json=None, data=None, headers=None):
            captured["json"] = json if json is not None else jsonlib.loads(data)
            captured["headers"] = headers
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
        def post(self, url, json=None, data=None, headers=None):
            captured["json"] = json if json is not None else jsonlib.loads(data)
            captured["headers"] = headers
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
        def post(self, url, json=None, data=None, headers=None):
            captured["json"] = json if json is not None else jsonlib.loads(data)
            captured["headers"] = headers
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


# ----------------------------------------------------------------------
# WP-268 Phase 2 audit fix — PII (telegram_id) удалён из payload + stable
# external_id (без epoch_ns) для user_updated/dt_oauth_completed/dt_recalc.
# ----------------------------------------------------------------------


def _read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_audit_fix_no_telegram_id_in_post_event_payloads():
    """Statically — никакой post_event payload в users/oauth/dt_sync не содержит
    `telegram_id`. Поиск по строкам формы `"telegram_id":` в payload-блоках.
    """
    project_root = Path(__file__).resolve().parents[2]
    targets = [
        project_root / "db" / "queries" / "users.py",
        project_root / "oauth_server.py",
        project_root / "db" / "queries" / "dt_sync.py",
    ]
    for path in targets:
        text = _read_text(path)
        # Грубый, но быстрый: ни в одном файле не должно быть строки
        # `"telegram_id": chat_id` или `"telegram_id": telegram_user_id`
        # в payload (PII-инвариант). Допустимо в SQL/log/комментариях.
        assert '"telegram_id": chat_id' not in text, (
            f"{path}: payload содержит raw telegram_id (PII)"
        )
        assert '"telegram_id": telegram_user_id' not in text, (
            f"{path}: payload содержит raw telegram_id (PII)"
        )


def test_audit_fix_no_epoch_ns_in_external_ids():
    """Statically — формула `int(now.timestamp() * 1_000_000_000)` удалена из
    всех 3 файлов (стабильные external_id вместо nanosecond timestamps).
    """
    project_root = Path(__file__).resolve().parents[2]
    targets = [
        project_root / "db" / "queries" / "users.py",
        project_root / "oauth_server.py",
        project_root / "db" / "queries" / "dt_sync.py",
    ]
    bad_patterns = [
        "timestamp() * 1_000_000_000",
        "timestamp()*1_000_000_000",
    ]
    for path in targets:
        text = _read_text(path)
        for pat in bad_patterns:
            assert pat not in text, f"{path}: остался epoch_ns pattern `{pat}`"
        # `epoch_ns = ...` присваивание тоже запрещено
        assert "epoch_ns = int(" not in text, (
            f"{path}: остался epoch_ns assignment"
        )


@pytest.mark.asyncio
async def test_tool_call_audit_v2_omits_sensitive_content_and_sets_account_id():
    """Д6.5: tool audit хранит только метаданные и канонического владельца."""
    from db.queries import traces

    telegram_user_id = 987654321
    account_id = "11111111-2222-3333-4444-555555555555"
    query_marker = "QUERY_SECRET_SENTINEL"
    available_tool_markers = [
        "SEARCH_TOOL_SECRET_SENTINEL",
        "PERSONAL_TOOL_SECRET_SENTINEL",
    ]
    chosen_tool_marker = available_tool_markers[1]
    input_key_marker = "INPUT_KEY_SECRET_SENTINEL"
    input_marker = "INPUT_SECRET_SENTINEL"
    result_marker = "RESULT_SECRET_SENTINEL"
    tool_input = {input_key_marker: input_marker, "limit": 5}

    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    class FakeAcquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    resolve_account = AsyncMock(return_value=account_id)
    with patch(
        "helpers.dual_write.resolve_ory_id_from_chat",
        resolve_account,
    ), patch.object(
        traces,
        "get_learning_pool",
        AsyncMock(return_value=FakePool()),
    ):
        await traces.log_tool_call_audit(
            telegram_user_id=telegram_user_id,
            query=query_marker,
            available_tools=available_tool_markers,
            chosen_tool=chosen_tool_marker,
            tool_input=tool_input,
            result_summary=result_marker,
        )

    resolve_account.assert_awaited_once_with(telegram_user_id)
    conn.execute.assert_awaited_once()
    (
        sql,
        source,
        external_id,
        event_type,
        schema_version,
        stored_account_id,
        occurred_at,
        serialized_payload,
    ) = conn.execute.await_args.args

    assert "account_id" in sql
    assert "$5::uuid" in sql
    assert source == "aist-bot"
    assert event_type == "tool_call_audit"
    assert schema_version == "v2"
    assert stored_account_id == account_id
    assert occurred_at.tzinfo == timezone.utc
    assert external_id.startswith("tool-audit-")
    assert len(external_id.removeprefix("tool-audit-")) == 32
    int(external_id.removeprefix("tool-audit-"), 16)

    sensitive_markers = (
        query_marker,
        *available_tool_markers,
        input_key_marker,
        input_marker,
        result_marker,
        str(telegram_user_id),
    )
    db_call_repr = repr(conn.execute.await_args.args)
    for marker in sensitive_markers:
        assert marker not in db_call_repr
        assert marker not in external_id
        assert marker not in serialized_payload

    payload = jsonlib.loads(serialized_payload)
    assert payload == {
        "available_tool_count": len(available_tool_markers),
        "available_tool_fingerprints": [
            hashlib.sha256(tool_name.encode("utf-8")).hexdigest()
            for tool_name in available_tool_markers
        ],
        "chosen_tool_fingerprint": hashlib.sha256(
            chosen_tool_marker.encode("utf-8")
        ).hexdigest(),
        "query_length": len(query_marker),
        "tool_input_key_count": len(tool_input),
        "tool_input_length": len(
            jsonlib.dumps(
                tool_input,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        ),
        "result_length": len(result_marker),
    }


@pytest.mark.asyncio
async def test_tool_call_audit_v2_skips_event_without_canonical_account_id():
    """Д6.5: событие без владельца не обходит RLS и контур удаления."""
    from db.queries import traces

    telegram_user_id = 987654321
    query_marker = "QUERY_SECRET_SENTINEL"
    tool_marker = "TOOL_SECRET_SENTINEL"
    input_marker = "INPUT_SECRET_SENTINEL"
    result_marker = "RESULT_SECRET_SENTINEL"

    conn = MagicMock()
    conn.execute = AsyncMock()
    acquire_context = MagicMock()
    acquire_context.__aenter__ = AsyncMock(return_value=conn)
    acquire_context.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire_context
    get_pool = AsyncMock(return_value=pool)
    resolve_account = AsyncMock(return_value=None)

    with patch(
        "helpers.dual_write.resolve_ory_id_from_chat",
        resolve_account,
    ), patch.object(
        traces,
        "get_learning_pool",
        get_pool,
    ), patch.object(
        traces.logger,
        "warning",
    ) as log_warning:
        await traces.log_tool_call_audit(
            telegram_user_id=telegram_user_id,
            query=query_marker,
            available_tools=[tool_marker],
            chosen_tool=tool_marker,
            tool_input={"query": input_marker},
            result_summary=result_marker,
        )

    resolve_account.assert_awaited_once_with(telegram_user_id)
    get_pool.assert_not_awaited()
    pool.acquire.assert_not_called()
    conn.execute.assert_not_awaited()
    log_warning.assert_called_once_with(
        "tool_call_audit skipped: canonical account_id unavailable"
    )
    warning_repr = repr(log_warning.call_args)
    for marker in (
        query_marker,
        tool_marker,
        input_marker,
        result_marker,
        str(telegram_user_id),
    ):
        assert marker not in warning_repr


@pytest.mark.asyncio
async def test_tool_call_audit_v2_malformed_input_never_raises_or_logs_content():
    """Д6.5: ошибка нормализации остаётся fire-and-forget и не раскрывает PII."""
    from db.queries import traces

    exception_marker = "EXCEPTION_SECRET_SENTINEL"
    telegram_id_marker = 987654321
    query_marker = "QUERY_SECRET_SENTINEL"

    class NonSerializableInput:
        def __len__(self):
            return 1

        def __str__(self):
            raise RuntimeError(exception_marker)

    resolve_account = AsyncMock(return_value="unused-account-id")
    get_pool = AsyncMock()
    with patch(
        "helpers.dual_write.resolve_ory_id_from_chat",
        resolve_account,
    ), patch.object(
        traces,
        "get_learning_pool",
        get_pool,
    ), patch.object(
        traces.logger,
        "warning",
    ) as log_warning:
        result = await traces.log_tool_call_audit(
            telegram_user_id=telegram_id_marker,
            query=query_marker,
            available_tools=["SAFE_TOOL"],
            chosen_tool="SAFE_TOOL",
            tool_input=NonSerializableInput(),
            result_summary="unused",
        )

    assert result is None
    resolve_account.assert_not_awaited()
    get_pool.assert_not_awaited()
    log_warning.assert_called_once_with(
        "tool_call_audit insert failed: %s",
        "RuntimeError",
    )
    warning_repr = repr(log_warning.call_args)
    for marker in (exception_marker, str(telegram_id_marker), query_marker):
        assert marker not in warning_repr


@pytest.mark.asyncio
async def test_tool_call_audit_task_callback_logs_exception_type_only():
    """Д6.5: callback фоновой задачи не пишет текст исключения или PII."""
    from engines.shared import question_handler

    exception_marker = "CALLBACK_EXCEPTION_SECRET_SENTINEL"
    telegram_id_marker = 987654321
    query_marker = "CALLBACK_QUERY_SECRET_SENTINEL"

    async def broken_audit(*args, **kwargs):
        raise RuntimeError(exception_marker)

    question_handler._bg_audit_tasks.clear()
    with patch.object(
        question_handler,
        "log_tool_call_audit",
        broken_audit,
    ), patch.object(
        question_handler.logger,
        "warning",
    ) as log_warning:
        question_handler._fire_and_forget_audit(
            telegram_user_id=telegram_id_marker,
            query=query_marker,
        )
        audit_tasks = tuple(question_handler._bg_audit_tasks)
        assert len(audit_tasks) == 1
        await asyncio.gather(*audit_tasks, return_exceptions=True)
        await asyncio.sleep(0)

    log_warning.assert_called_once_with(
        "tool_call_audit task failed: %s",
        "RuntimeError",
    )
    warning_repr = repr(log_warning.call_args)
    for marker in (exception_marker, str(telegram_id_marker), query_marker):
        assert marker not in warning_repr


@pytest.mark.asyncio
async def test_user_updated_payload_omits_telegram_id():
    """`user_updated` payload содержит только fields_updated/fields_count,
    без `telegram_id`. external_id — стабильный hash (не epoch_ns).
    """
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
        def post(self, url, json=None, data=None, headers=None):
            captured["json"] = json if json is not None else jsonlib.loads(data)
            captured["headers"] = headers
            return FakeResponse()

    # Эмулируем тот же envelope, который собирает update_intern после fix
    chat_id = 555
    affected_fields = ["mode", "schedule_time"]
    fields_hash = "abc123def456"  # любой стабильный hash для теста envelope
    with patch.object(dual_write, "_get_session", return_value=FakeSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            await dual_write.post_event(
                source="aist-bot",
                external_id=f"user-updated-{chat_id}-{fields_hash}",
                event_type="user_updated",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id="ory-uuid-555",
                payload={
                    "fields_updated": affected_fields,
                    "fields_count": len(affected_fields),
                },
            )

    env = captured["json"]
    assert env["event_type"] == "user_updated"
    # PII инвариант: telegram_id отсутствует
    assert "telegram_id" not in env["payload"]
    # account_id — это ory_id (из resolve)
    assert env["account_id"] == "ory-uuid-555"
    # external_id — стабильный (нет nanosecond timestamp)
    assert "user-updated-555-" in env["external_id"]
    assert "1_000_000_000" not in env["external_id"]


@pytest.mark.asyncio
async def test_dt_oauth_completed_payload_omits_telegram_id():
    """`dt_oauth_completed` payload без `telegram_id`, external_id стабильный
    (один на OAuth flow), account_id — ory_id (или None для T0 fallback).
    """
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
        def post(self, url, json=None, data=None, headers=None):
            captured["json"] = json if json is not None else jsonlib.loads(data)
            captured["headers"] = headers
            return FakeResponse()

    ory_id = "11111111-2222-3333-4444-555555555555"
    with patch.object(dual_write, "_get_session", return_value=FakeSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            await dual_write.post_event(
                source="aist-bot",
                external_id=f"dt-oauth-completed-{ory_id}",
                event_type="dt_oauth_completed",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id=ory_id,
                payload={"via": "dt_oauth_callback"},
            )

    env = captured["json"]
    assert env["event_type"] == "dt_oauth_completed"
    assert "telegram_id" not in env["payload"]
    assert env["account_id"] == ory_id
    # external_id keyed by ory_id (один на flow)
    assert env["external_id"] == f"dt-oauth-completed-{ory_id}"


@pytest.mark.asyncio
async def test_dt_recalc_single_uses_day_bucket():
    """`dt_recalc` (single mode) external_id содержит hourly bucket (YYYY-MM-DDTHH),
    не epoch_ns. Идемпотентен на уровне часа.
    """
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
        def post(self, url, json=None, data=None, headers=None):
            captured["json"] = json if json is not None else jsonlib.loads(data)
            captured["headers"] = headers
            return FakeResponse()

    user_uuid = "abc-def-123"
    day_bucket = "2026-04-26T12"
    with patch.object(dual_write, "_get_session", return_value=FakeSession()):
        with patch.object(dual_write, "EVENT_GATEWAY_ENABLED", True):
            await dual_write.post_event(
                source="aist-bot",
                external_id=f"dt-recalc-{user_uuid}-{day_bucket}",
                event_type="dt_recalc",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id=user_uuid,
                payload={"mode": "single", "user_id": user_uuid, "sections_written": []},
            )

    env = captured["json"]
    assert env["event_type"] == "dt_recalc"
    assert env["payload"]["mode"] == "single"
    # external_id keyed by hourly bucket
    assert env["external_id"] == f"dt-recalc-{user_uuid}-{day_bucket}"
    # Никакого nanosecond timestamp
    assert "000000000" not in env["external_id"]


# ----------------------------------------------------------------------
# WP-268 Phase 2 Issue 5 fix (verifier subagent a321d6bc, 26 апр 2026)
# user_registered.v1 — единственный owner = identity.py:get_or_create_user.
# До фикса дублировался в users.py:get_intern (UNIQUE constraint защищал в БД,
# но 2 кодопути для одного факта = noise + размытие source-of-truth).
# ----------------------------------------------------------------------

import re


def _grep_event_emits(file_path: Path, event_type: str) -> list:
    """Найти строки с `event_type="X"` в файле. Возвращает список номеров строк."""
    pattern = re.compile(rf'event_type\s*=\s*["\']({re.escape(event_type)})["\']')
    matches = []
    text = file_path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        if pattern.search(line):
            matches.append(i)
    return matches


def test_issue5_user_registered_emit_only_in_identity():
    """Issue 5: user_registered.v1 эмитится ТОЛЬКО из identity.py.

    Регрессия-страж против повторного добавления emit'а в users.py.
    """
    project_root = Path(__file__).resolve().parents[2]
    users_py = project_root / "db" / "queries" / "users.py"
    identity_py = project_root / "db" / "queries" / "identity.py"

    users_emits = _grep_event_emits(users_py, "user_registered")
    identity_emits = _grep_event_emits(identity_py, "user_registered")

    assert users_emits == [], (
        f"Issue 5 регрессия: user_registered эмитится из users.py "
        f"строки={users_emits}. SoT для регистрации = identity.py:get_or_create_user."
    )
    assert len(identity_emits) >= 1, (
        f"user_registered НЕ эмитится из identity.py — пропал owner-эмит. "
        f"Ожидается ≥1 строка с event_type='user_registered' в "
        f"identity.py:get_or_create_user."
    )


def test_issue5_no_duplicate_external_id_pattern():
    """external_id pattern `user-registered-{user_id}` живёт ТОЛЬКО в identity.py."""
    project_root = Path(__file__).resolve().parents[2]
    users_py = project_root / "db" / "queries" / "users.py"
    identity_py = project_root / "db" / "queries" / "identity.py"

    users_text = users_py.read_text(encoding="utf-8")
    identity_text = identity_py.read_text(encoding="utf-8")

    pat = re.compile(r'user-registered-\{')
    users_matches = pat.findall(users_text)
    identity_matches = pat.findall(identity_text)

    assert users_matches == [], (
        f"users.py содержит pattern 'user-registered-{{...}}' "
        f"({len(users_matches)} совпадений). После Issue 5 fix — должно быть 0."
    )
    assert len(identity_matches) >= 1, (
        "identity.py НЕ содержит pattern 'user-registered-{...}' — "
        "регрессия Issue 5 fix (owner-emit пропал)."
    )


def test_issue5_users_py_still_emits_user_updated():
    """Regression guard: users.py остаётся owner для user_updated."""
    project_root = Path(__file__).resolve().parents[2]
    users_py = project_root / "db" / "queries" / "users.py"
    user_updated = _grep_event_emits(users_py, "user_updated")
    assert len(user_updated) >= 1, (
        "users.py больше НЕ эмитит user_updated — Issue 5 fix удалил лишнее. "
        "Ожидается ≥1 emit в update_intern path."
    )
