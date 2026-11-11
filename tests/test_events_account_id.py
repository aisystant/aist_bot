"""Bot events reach the central journal under the platform account id (WP-5 Ф74)."""

import asyncio
from types import SimpleNamespace

import pytest

from db.queries import events

ORY_ID = "25d91dbb-0000-4000-8000-000000000001"


@pytest.fixture
def wiring(monkeypatch):
    """Replace I/O around _record_event; collect gateway writes."""
    state = SimpleNamespace(posted=[], legacy=(101, False), consent=None, ory=ORY_ID)

    async def resolve(chat_id):
        return state.ory

    async def insert_legacy(*args, **kwargs):
        return state.legacy

    async def post_event(**kwargs):
        state.posted.append(kwargs)

    async def get_consent(account_id):
        state.consent_lookup = account_id
        return state.consent

    monkeypatch.setattr("helpers.dual_write.resolve_ory_id_from_chat", resolve)
    monkeypatch.setattr("db.queries.consent.get_consent", get_consent)
    monkeypatch.setattr(events, "_insert_legacy_event", insert_legacy)
    monkeypatch.setattr(events, "post_event", post_event)
    return state


async def _flush():
    await asyncio.sleep(0)


async def test_gateway_gets_platform_account_id(wiring):
    event_id = await events.log_event(42, "progress_viewed", {"view_type": "short"})
    await _flush()

    assert event_id == 101
    assert len(wiring.posted) == 1
    assert wiring.posted[0]["account_id"] == ORY_ID
    assert wiring.posted[0]["payload"]["activity_domain"] == "work"
    assert wiring.consent_lookup == ORY_ID


async def test_gateway_written_when_legacy_insert_fails(wiring):
    wiring.legacy = (None, False)

    event_id = await events.log_event(42, "session_start")
    await _flush()

    assert event_id is None
    assert [p["event_type"] for p in wiring.posted] == ["session_start"]


async def test_opt_out_by_platform_account_writes_nothing(wiring):
    wiring.consent = SimpleNamespace(opt_in=False)

    assert await events.log_event(42, "session_start") is None
    await _flush()

    assert wiring.posted == []


async def test_dedup_skip_does_not_duplicate_in_journal(wiring):
    wiring.legacy = (None, True)

    assert await events.log_event(42, "session_start") is None
    await _flush()

    assert wiring.posted == []


async def test_backfill_keeps_occurred_at(wiring):
    from datetime import datetime

    past = datetime(2026, 11, 1, 9, 30)
    await events.log_event_with_occurred_at(42, "slot_logged", {"source": "self_report_backfill"}, occurred_at=past)
    await _flush()

    assert wiring.posted[0]["occurred_at"] == past
    assert wiring.posted[0]["account_id"] == ORY_ID
