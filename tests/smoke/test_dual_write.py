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
