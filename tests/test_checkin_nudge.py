"""Missed check-in reminders: at most two per episode (РП-568 Ф16, DP.SC.116).

Pilot rule 11.11.2026: first reminder the day after the miss, second three days
after the first (miss on Monday → Tuesday and Friday), then silence.
"""
from datetime import date, datetime, timezone

import pytest

import core.scheduler as scheduler
from core.checkin_nudge import due_stage, journal_key

MON = date(2026, 11, 9)   # first missed day M
TUE, WED, THU, FRI, SAT, SUN = (date(2026, 11, d) for d in range(10, 16))
NEXT_MON = date(2026, 11, 16)


def test_stage1_the_day_after_the_miss():
    assert due_stage(TUE, MON, stage1_sent_on=None, stage2_sent=False) == 1


def test_nothing_on_the_missed_day_itself():
    assert due_stage(MON, MON, stage1_sent_on=None, stage2_sent=False) is None


def test_stage1_survives_a_skipped_run():
    # The 10:00 run on Tuesday did not happen → Wednesday still sends stage 1.
    assert due_stage(WED, MON, stage1_sent_on=None, stage2_sent=False) == 1


def test_no_stage1_after_its_window():
    assert due_stage(FRI, MON, stage1_sent_on=None, stage2_sent=False) is None


def test_pilot_example_tuesday_then_friday():
    assert due_stage(WED, MON, stage1_sent_on=TUE, stage2_sent=False) is None
    assert due_stage(THU, MON, stage1_sent_on=TUE, stage2_sent=False) is None
    assert due_stage(FRI, MON, stage1_sent_on=TUE, stage2_sent=False) == 2


def test_stage2_counts_from_actual_delivery_of_stage1():
    # Stage 1 went out late (Thursday) → stage 2 not before Sunday, never the next day.
    assert due_stage(FRI, MON, stage1_sent_on=THU, stage2_sent=False) is None
    assert due_stage(SUN, MON, stage1_sent_on=THU, stage2_sent=False) == 2


def test_silence_after_stage2_window_and_after_stage2():
    assert due_stage(NEXT_MON, MON, stage1_sent_on=TUE, stage2_sent=False) is None
    assert due_stage(SAT, MON, stage1_sent_on=TUE, stage2_sent=True) is None


def test_new_episode_gets_its_own_keys():
    assert journal_key(42, MON, 1) == "marathon_nudge:42:2026-11-09:1"
    assert journal_key(42, THU, 1) != journal_key(42, MON, 1)


# ── the scheduler job ──

class _Recorder:
    def __init__(self):
        self.calls = []

    async def enqueue(self, chat_id, klass, content_spec, **kwargs):
        self.calls.append({"chat_id": chat_id, "klass": klass,
                           "text": content_spec["text"], **kwargs})
        return {"notification_id": 1, "status": "queued"}


def _patch_job(monkeypatch, today, batch_done, episodes, sent_at=None, sent_keys=()):
    import core.notification_service as ns
    import db.queries.marathon_newcomer as mn
    import db.queries.notifications as notif

    rec = _Recorder()
    monkeypatch.setattr(ns, "enqueue", rec.enqueue)
    monkeypatch.setattr(scheduler, "_bot_token", "token")
    monkeypatch.setattr(scheduler, "DEVELOPER_CHAT_ID", "999")
    monkeypatch.setattr(
        scheduler, "moscow_now",
        lambda: datetime(today.year, today.month, today.day, 10, 0, tzinfo=scheduler.MOSCOW_TZ),
    )

    async def _batch_done(_day):
        return batch_done

    async def _episodes(_today):
        return episodes

    async def _sent_at(key):
        return (sent_at or {}).get(key)

    async def _was_sent(key):
        return key in sent_keys

    monkeypatch.setattr(mn, "was_activity_batch_done", _batch_done)
    monkeypatch.setattr(mn, "get_checkin_episodes", _episodes)
    monkeypatch.setattr(notif, "get_notification_sent_at", _sent_at)
    monkeypatch.setattr(notif, "was_notification_sent", _was_sent)
    return rec


@pytest.mark.asyncio
async def test_job_sends_nothing_to_participants_without_nightly_batch(monkeypatch):
    rec = _patch_job(monkeypatch, TUE, batch_done=False,
                     episodes=[{"user_id": 42, "episode_start": MON}])
    await scheduler._send_marathon_nudges()
    assert [c["chat_id"] for c in rec.calls] == [999]          # one ops line only
    assert rec.calls[0]["dedup_key"] == "checkin-nudge-batch-missing:2026-11-09"


@pytest.mark.asyncio
async def test_job_sends_stage1_with_episode_key(monkeypatch):
    rec = _patch_job(monkeypatch, TUE, batch_done=True,
                     episodes=[{"user_id": 42, "episode_start": MON}])
    await scheduler._send_marathon_nudges()
    assert len(rec.calls) == 1
    assert rec.calls[0]["chat_id"] == 42
    assert rec.calls[0]["journal_key"] == "marathon_nudge:42:2026-11-09:1"
    assert rec.calls[0]["journal_type"] == "marathon_nudge"
    assert "Небольшая пауза" in rec.calls[0]["text"]


@pytest.mark.asyncio
async def test_job_sends_stage2_three_days_after_stage1_then_stops(monkeypatch):
    key1 = journal_key(42, MON, 1)
    # Stage 1 delivered Tuesday 07:05 UTC = 10:05 MSK.
    sent = {key1: datetime(2026, 11, 10, 7, 5, tzinfo=timezone.utc)}
    episodes = [{"user_id": 42, "episode_start": MON}]

    rec = _patch_job(monkeypatch, THU, True, episodes, sent_at=sent, sent_keys={key1})
    await scheduler._send_marathon_nudges()
    assert rec.calls == []

    rec = _patch_job(monkeypatch, FRI, True, episodes, sent_at=sent, sent_keys={key1})
    await scheduler._send_marathon_nudges()
    assert [c["journal_key"] for c in rec.calls] == ["marathon_nudge:42:2026-11-09:2"]
    assert "Проверка связи" in rec.calls[0]["text"]

    key2 = journal_key(42, MON, 2)
    rec = _patch_job(monkeypatch, SAT, True, episodes, sent_at=sent, sent_keys={key1, key2})
    await scheduler._send_marathon_nudges()
    assert rec.calls == []


@pytest.mark.asyncio
async def test_job_counts_pre_deploy_daily_key_as_stage1(monkeypatch):
    # Before the change the reminder was journaled per day; one sent on Tuesday
    # inside the episode is stage 1, so Wednesday's first run does not repeat it.
    legacy = "marathon_nudge:42:2026-11-10"
    rec = _patch_job(monkeypatch, WED, True, [{"user_id": 42, "episode_start": MON}],
                     sent_at={legacy: datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)})
    await scheduler._send_marathon_nudges()
    assert rec.calls == []

    rec = _patch_job(monkeypatch, FRI, True, [{"user_id": 42, "episode_start": MON}],
                     sent_at={legacy: datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)})
    await scheduler._send_marathon_nudges()
    assert [c["journal_key"] for c in rec.calls] == ["marathon_nudge:42:2026-11-09:2"]
