"""WP-568 Ф13: club publisher facts for the platform event journal.

The payloads below must stay valid against event-gateway's
club_publication.v1 / club_publication_queue_low.v1 (closed schemas): an extra
field or a missing bot_instance is rejected by the gateway and the outbox row
retries forever.
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.club_publisher_events as events
from clients.discourse import DiscourseError

PILOT_CHAT = 111
OTHER_CHAT = 222


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def fake_enqueue(chat_id, external_id, event_type, payload):
        calls.append({"chat_id": chat_id, "external_id": external_id, "event_type": event_type, "payload": payload})

    monkeypatch.setattr(events, "_enqueue", fake_enqueue)
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


def test_routes_via_center_only_for_pilot_when_both_switches_on(monkeypatch):
    monkeypatch.setattr(events, "DEVELOPER_CHAT_ID", PILOT_CHAT)
    monkeypatch.setattr(events, "CLUB_PUBLISHER_EVENTS_INSTANCE", "pilot")
    monkeypatch.setattr(events, "CLUB_PUBLISHER_PILOT_VIA_CENTER", True)
    assert events.routes_via_center(PILOT_CHAT) is True
    assert events.routes_via_center(OTHER_CHAT) is False

    monkeypatch.setattr(events, "CLUB_PUBLISHER_PILOT_VIA_CENTER", False)
    assert events.routes_via_center(PILOT_CHAT) is False

    # Without events the center has nothing to read: never silence the bot.
    monkeypatch.setattr(events, "CLUB_PUBLISHER_PILOT_VIA_CENTER", True)
    monkeypatch.setattr(events, "CLUB_PUBLISHER_EVENTS_INSTANCE", "")
    assert events.routes_via_center(PILOT_CHAT) is False


async def test_scheduled_fact_carries_slot_and_slot_in_key(captured):
    await events.record_publication(
        OTHER_CHAT, status="scheduled", publication_id=7, title="Пост", scheduled_for=datetime(2026, 11, 12, 7, 0),
    )
    [call] = captured
    assert call["event_type"] == "club_publication"
    assert call["payload"] == {
        "status": "scheduled", "publication_id": 7, "title": "Пост", "bot_instance": "pilot",
        "scheduled_for": "2026-11-12T07:00:00Z",
    }
    assert call["external_id"] == "club-publication:pilot:7:scheduled:20261112T0700"


async def test_failed_fact_has_failure_class_and_per_attempt_key(captured):
    await events.record_publication(OTHER_CHAT, status="failed", publication_id=7, title="Пост", failure="club_unreachable")
    [call] = captured
    assert call["payload"]["failure"] == "club_unreachable"
    assert set(call["payload"]) == {"status", "publication_id", "title", "bot_instance", "failure"}
    assert call["external_id"].startswith("club-publication:pilot:7:failed:")
    assert len(call["external_id"].rsplit(":", 1)[1]) == len("20261109T1000")


async def test_published_fact_keeps_url_and_truncates_long_title(captured):
    await events.record_publication(
        OTHER_CHAT, status="published", publication_id=7, title="я" * 400, topic_url="https://systemsworld.club/t/x/1",
    )
    [call] = captured
    assert len(call["payload"]["title"]) == 300
    assert call["payload"]["topic_url"] == "https://systemsworld.club/t/x/1"
    assert call["external_id"] == "club-publication:pilot:7:published"


async def test_queue_low_fact_one_per_author_per_day(captured):
    await events.record_queue_low(OTHER_CHAT, pending_count=1, min_queue=2, day=date(2026, 11, 9))
    [call] = captured
    assert call["event_type"] == "club_publication_queue_low"
    assert call["payload"] == {"pending_count": 1, "min_queue": 2, "bot_instance": "pilot"}
    assert call["external_id"] == f"club-publication-queue-low:pilot:{OTHER_CHAT}:2026-11-09"


async def test_nothing_recorded_when_events_off(captured, monkeypatch):
    monkeypatch.setattr(events, "CLUB_PUBLISHER_EVENTS_INSTANCE", "")
    await events.record_publication(OTHER_CHAT, status="published", publication_id=7, title="Пост")
    await events.record_queue_low(OTHER_CHAT, pending_count=0, min_queue=2, day=date(2026, 11, 9))
    assert captured == []


async def test_enqueue_failure_is_logged_not_raised(monkeypatch, caplog):
    async def broken_resolve(_chat_id):
        raise ConnectionError("db down")

    import helpers.dual_write as dual_write

    monkeypatch.setattr(dual_write, "resolve_ory_id_from_chat", broken_resolve)
    await events._enqueue(OTHER_CHAT, "club-publication:pilot:7:published", "club_publication", {})
    assert "enqueue failed club-publication:pilot:7:published" in caplog.text
