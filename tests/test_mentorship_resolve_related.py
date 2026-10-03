"""
WP-578 Ф2 — _resolve_related_participant должен ИСКАТЬ, не создавать
participant_core для reply/forward целей (холодное ревью 17.09: исходная
версия использовала get_or_create_participant и молча заводила посторонних
как участников потока).
"""

import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_related_participant_does_not_call_create(monkeypatch):
    import engines.mentorship.archive_tap as archive_tap
    import db.queries.mentorship as mentorship_queries

    resolve_mock = AsyncMock(return_value="bystander-account-id")
    monkeypatch.setattr(archive_tap, "resolve_ory_id_from_chat", resolve_mock)

    find_mock = AsyncMock(return_value=None)  # посторонний не найден в этом потоке
    monkeypatch.setattr(mentorship_queries, "find_participant_id", find_mock)

    create_mock = AsyncMock(side_effect=AssertionError("не должен вызываться для чужого reply-target"))
    monkeypatch.setattr(mentorship_queries, "get_or_create_participant", create_mock)

    result = await archive_tap._resolve_related_participant(
        999,
        reader_account_id="reader-1",
        stream_id="S1",
        exclude_account_id="participant-1",
    )

    assert result is None
    find_mock.assert_awaited_once_with("reader-1", "S1", "bystander-account-id")
    create_mock.assert_not_called()


@pytest.mark.asyncio
async def test_related_participant_returns_existing_id(monkeypatch):
    import engines.mentorship.archive_tap as archive_tap
    import db.queries.mentorship as mentorship_queries

    monkeypatch.setattr(archive_tap, "resolve_ory_id_from_chat", AsyncMock(return_value="known-account-id"))
    monkeypatch.setattr(mentorship_queries, "find_participant_id", AsyncMock(return_value=42))

    result = await archive_tap._resolve_related_participant(
        555,
        reader_account_id="reader-1",
        stream_id="S1",
        exclude_account_id="participant-1",
    )

    assert result == 42


@pytest.mark.asyncio
async def test_related_participant_excludes_self(monkeypatch):
    import engines.mentorship.archive_tap as archive_tap
    import db.queries.mentorship as mentorship_queries

    monkeypatch.setattr(archive_tap, "resolve_ory_id_from_chat", AsyncMock(return_value="participant-1"))
    find_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(mentorship_queries, "find_participant_id", find_mock)

    result = await archive_tap._resolve_related_participant(
        111,
        reader_account_id="reader-1",
        stream_id="S1",
        exclude_account_id="participant-1",
    )

    assert result is None
    find_mock.assert_not_called()
