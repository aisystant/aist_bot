"""WP-568 Ф13: club publisher facts for the platform event journal.

The payloads below must stay valid against event-gateway's
club_publication.v1 / club_publication_queue_low.v1 (closed schemas): an extra
field or a missing bot_instance is rejected by the gateway for good.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.club_publisher_events as events
from clients.discourse import DiscourseError

PILOT_CHAT = 111
OTHER_CHAT = 222333444
ACCOUNT = "25d91dbb-0000-4000-8000-000000000001"


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def fake_enqueue(external_id, event_type, account_id, payload):
        calls.append({"external_id": external_id, "event_type": event_type, "account_id": account_id, "payload": payload})
        return True

    async def fake_account(_chat_id):
        return ACCOUNT

    monkeypatch.setattr(events, "_enqueue", fake_enqueue)
    monkeypatch.setattr(events, "_resolve_account", fake_account)
    monkeypatch.setattr(events, "CLUB_PUBLISHER_EVENTS_INSTANCE", "pilot")
    monkeypatch.setattr(events, "DEVELOPER_CHAT_ID", PILOT_CHAT)
    return calls


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (DiscourseError("HTTP 422 (invalid_parameters): Title has already been used"), "club_rejected"),
        (DiscourseError("HTTP 503: ответ не JSON"), "club_unreachable"),
        (DiscourseError("HTTP 429 (rate_limit): slow down"), "club_unreachable"),
        (DiscourseError("Discourse POST failed after retries: Cannot connect to host"), "club_unreachable"),
        (RuntimeError("bug"), "unknown"),
    ],
)
def test_classify_publish_failure(exc, expected):
    assert events.classify_publish_failure(exc) == expected


def test_routes_via_center_only_for_pilot_when_all_switches_on(monkeypatch):
    monkeypatch.setattr(events, "DEVELOPER_CHAT_ID", PILOT_CHAT)
    monkeypatch.setattr(events, "CLUB_PUBLISHER_EVENTS_INSTANCE", "pilot")
    monkeypatch.setattr(events, "CLUB_PUBLISHER_PILOT_VIA_CENTER", True)
    monkeypatch.setattr(events, "EVENT_GATEWAY_ENABLED", True)
    assert events.routes_via_center(PILOT_CHAT) is True
    assert events.routes_via_center(OTHER_CHAT) is False

    monkeypatch.setattr(events, "CLUB_PUBLISHER_PILOT_VIA_CENTER", False)
    assert events.routes_via_center(PILOT_CHAT) is False

    # Without events reaching the journal the center has nothing to read.
    monkeypatch.setattr(events, "CLUB_PUBLISHER_PILOT_VIA_CENTER", True)
    monkeypatch.setattr(events, "EVENT_GATEWAY_ENABLED", False)
    assert events.routes_via_center(PILOT_CHAT) is False
    monkeypatch.setattr(events, "EVENT_GATEWAY_ENABLED", True)
    monkeypatch.setattr(events, "CLUB_PUBLISHER_EVENTS_INSTANCE", "")
    assert events.routes_via_center(PILOT_CHAT) is False


async def test_scheduled_fact_carries_slot_account_and_no_telegram_id(captured):
    ok = await events.record_publication(
        OTHER_CHAT, status="scheduled", publication_id=7, title="Пост", scheduled_for=datetime(2026, 11, 12, 7, 0),
    )
    [call] = captured
    assert ok is True
    assert call["event_type"] == "club_publication"
    assert call["account_id"] == ACCOUNT
    assert call["payload"] == {
        "status": "scheduled", "publication_id": 7, "title": "Пост", "bot_instance": "pilot",
        "scheduled_for": "2026-11-12T07:00:00Z",
    }
    assert call["external_id"] == "club-publication:pilot:7:scheduled:20261112T0700"
    assert str(OTHER_CHAT) not in call["external_id"]


async def test_aware_slot_is_converted_not_relabelled(captured):
    msk = timezone(timedelta(hours=3))
    await events.record_publication(
        OTHER_CHAT, status="scheduled", publication_id=7, title="Пост", scheduled_for=datetime(2026, 11, 12, 10, 0, tzinfo=msk),
    )
    assert captured[0]["payload"]["scheduled_for"] == "2026-11-12T07:00:00Z"


async def test_failed_fact_has_failure_class_and_per_attempt_key(captured):
    await events.record_publication(OTHER_CHAT, status="failed", publication_id=7, title="Пост", failure="club_unreachable")
    [call] = captured
    assert call["payload"]["failure"] == "club_unreachable"
    assert set(call["payload"]) == {"status", "publication_id", "title", "bot_instance", "failure"}
    assert call["external_id"].startswith("club-publication:pilot:7:failed:")
    assert len(call["external_id"].rsplit(":", 1)[1]) == len("20261109T1000")


async def test_published_fact_keeps_url_and_bounds_title(captured):
    await events.record_publication(
        OTHER_CHAT, status="published", publication_id=7, title="я" * 400, topic_url="https://systemsworld.club/t/x/1",
    )
    await events.record_publication(OTHER_CHAT, status="published", publication_id=8, title=None)
    assert len(captured[0]["payload"]["title"]) == 300
    assert captured[0]["payload"]["topic_url"] == "https://systemsworld.club/t/x/1"
    assert captured[0]["external_id"] == "club-publication:pilot:7:published"
    assert captured[1]["payload"]["title"] == "(без названия)"


async def test_queue_low_key_uses_platform_account_not_telegram_id(captured):
    ok = await events.record_queue_low(OTHER_CHAT, pending_count=1, min_queue=2, day=date(2026, 11, 9))
    [call] = captured
    assert ok is True
    assert call["event_type"] == "club_publication_queue_low"
    assert call["payload"] == {"pending_count": 1, "min_queue": 2, "bot_instance": "pilot"}
    assert call["external_id"] == f"club-publication-queue-low:pilot:{ACCOUNT}:2026-11-09"
    assert str(OTHER_CHAT) not in call["external_id"]


async def test_without_platform_account_nothing_routes_via_center(captured, monkeypatch):
    async def no_account(_chat_id):
        return None

    monkeypatch.setattr(events, "_resolve_account", no_account)
    assert await events.record_queue_low(PILOT_CHAT, pending_count=0, min_queue=2, day=date(2026, 11, 9)) is False
    assert captured == []  # no PII-free key -> no event at all
    # The publication fact is still a domain fact, but the caller must send directly.
    assert await events.record_publication(PILOT_CHAT, status="published", publication_id=7, title="Пост") is False
    assert len(captured) == 1


async def test_failed_enqueue_reports_false(captured, monkeypatch):
    async def broken_enqueue(*_args):
        return False

    monkeypatch.setattr(events, "_enqueue", broken_enqueue)
    assert await events.record_publication(PILOT_CHAT, status="published", publication_id=7, title="Пост") is False


async def test_nothing_recorded_when_events_off(captured, monkeypatch):
    monkeypatch.setattr(events, "CLUB_PUBLISHER_EVENTS_INSTANCE", "")
    assert await events.record_publication(OTHER_CHAT, status="published", publication_id=7, title="Пост") is False
    assert await events.record_queue_low(OTHER_CHAT, pending_count=0, min_queue=2, day=date(2026, 11, 9)) is False
    assert captured == []


async def test_enqueue_failure_is_logged_not_raised(monkeypatch, caplog):
    import db.connection as db_connection

    async def broken_pool():
        raise ConnectionError("db down")

    monkeypatch.setattr(db_connection, "get_pool", broken_pool)
    assert await events._enqueue("club-publication:pilot:7:published", "club_publication", None, {}) is False
    assert "enqueue failed club-publication:pilot:7:published" in caplog.text


def test_outbox_serves_fewest_attempts_first():
    """A permanently rejected row must not hold the head of the queue."""
    import inspect

    from db.queries import event_outbox

    sql = inspect.getsource(event_outbox.fetch_pending_outbox)
    assert "ORDER BY attempts ASC, created_at ASC" in sql
