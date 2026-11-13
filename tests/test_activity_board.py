"""Ecosystem activity folded into the /progress overview (WP-417 Ф7)."""

import asyncio
import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

from clients.gateway_mcp import gateway_mcp
from core.activity_board import CHANNEL_BITS, apply_ecosystem, apply_totals, day_masks
from integrations.telegram.widgets import build_progress_chat_data
from states.utilities.progress import ProgressState

TODAY = date(2026, 11, 12)  # Thursday
ACCOUNT = "00000000-0000-0000-0000-000000000042"  # linked platform account (intern dt_user_id)


def _calendar(days: int, bot_active: set[int] = frozenset()) -> list[dict]:
    """`days` consecutive dates ending TODAY; bot_active = offsets from the first day."""
    start = TODAY - timedelta(days=days - 1)
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "weekday": (start + timedelta(days=i)).weekday(),
         "active": i in bot_active}
        for i in range(days)
    ]


def _row(day: date, channel: str, events: int = 1) -> dict:
    return {"day": day.isoformat(), "channel": channel, "events": events}


def test_day_masks_combine_places_and_ignore_unknown_or_empty():
    masks = day_masks([
        _row(TODAY, "iwe", 5), _row(TODAY, "club", 1), _row(TODAY, "other", 9),
        _row(TODAY - timedelta(days=1), "lms", 0), _row(TODAY - timedelta(days=2), "bot", 2),
    ])

    assert masks == {TODAY.isoformat(): CHANNEL_BITS["iwe"] | CHANNEL_BITS["club"],
                     (TODAY - timedelta(days=2)).isoformat(): CHANNEL_BITS["bot"]}


def test_apply_marks_days_with_their_places_and_keeps_bot_days_active():
    cache = {"calendar": _calendar(5, bot_active={0})}  # 08.11 .. 12.11, bot active on 08.11 only

    apply_ecosystem(cache, [_row(TODAY, "iwe"), _row(TODAY - timedelta(days=1), "club")])

    masks = [entry["mask"] for entry in cache["calendar"]]
    active = [entry["active"] for entry in cache["calendar"]]
    assert masks == [0, 0, 0, CHANNEL_BITS["club"], CHANNEL_BITS["iwe"]]
    assert active == [True, False, False, True, True]
    assert cache["ecosystem"] is True and cache["calendar_active_count"] == 3


def test_apply_counts_week_from_monday_and_streak_with_open_today():
    cache = {"calendar": _calendar(10)}  # 03.11 .. 12.11; Monday of today's week is 09.11
    days = [_row(TODAY - timedelta(days=n), "iwe") for n in (1, 2, 3, 5)]  # 11, 10, 09 and 07 Nov

    apply_ecosystem(cache, days)

    assert cache["days_active_week"] == 3          # 09, 10, 11 (today still empty)
    assert cache["streak"] == 3                    # run ending yesterday stays current
    assert cache["longest_streak"] == 3


def test_apply_never_lowers_what_the_bot_already_knew():
    cache = {"calendar": _calendar(3), "streak": 40, "longest_streak": 90, "active_days_total": 500}

    apply_ecosystem(cache, [_row(TODAY, "iwe")])

    assert (cache["streak"], cache["longest_streak"], cache["active_days_total"]) == (40, 90, 500)


def test_apply_raises_counters_when_the_ecosystem_knows_more():
    cache = {"calendar": _calendar(4), "streak": 1, "longest_streak": 1, "active_days_total": 2}

    apply_ecosystem(cache, [_row(TODAY - timedelta(days=n), "iwe") for n in range(4)])

    assert (cache["streak"], cache["longest_streak"], cache["active_days_total"]) == (4, 4, 4)


def test_apply_without_calendar_changes_nothing():
    cache = {"streak": 2}

    apply_ecosystem(cache, [_row(TODAY, "iwe")])

    assert cache == {"streak": 2}


def test_chat_data_carries_places_per_day_and_fits_the_limit():
    cache = {"streak": 28, "longest_streak": 28, "active_days_total": 99999, "points_balance": 10**9,
             "calendar": _calendar(35)}
    apply_ecosystem(cache, [_row(TODAY - timedelta(days=n), channel) for n in range(35) for channel in CHANNEL_BITS])

    encoded = build_progress_chat_data(cache, "ru")

    assert json.loads(encoded)["m"] == "f" * 35
    assert len(encoded.encode()) <= 1024


def test_chat_data_has_no_places_without_ecosystem_data():
    cache = {"calendar": _calendar(3)}

    assert "m" not in json.loads(build_progress_chat_data(cache, "ru"))


async def test_gateway_tool_returns_days_and_passes_the_period():
    answer = {"days": [_row(TODAY, "iwe")], "period": {"from": "2026-10-15", "to": "2026-11-12"}}
    result = {"content": [{"type": "text", "text": json.dumps(answer)}]}
    with patch.object(gateway_mcp, "_call", AsyncMock(return_value=result)) as call:
        got = await gateway_mcp.dt_read_activity(42, "2026-10-15", "2026-11-12")

    assert got == answer
    call.assert_awaited_once_with("dt_read_activity", {"from": "2026-10-15", "to": "2026-11-12"}, 42, timeout=5,
                                 best_effort=True)


async def test_gateway_tool_returns_none_on_errors_and_odd_answers():
    for result in (None, {"isError": True, "content": [{"type": "text", "text": "Backend error: x"}]},
                   {"content": [{"type": "text", "text": "[]"}]},
                   {"content": [{"type": "text", "text": json.dumps({"days": "no"})}]}):
        with patch.object(gateway_mcp, "_call", AsyncMock(return_value=result)):
            assert await gateway_mcp.dt_read_activity(42) is None


async def test_progress_fetch_asks_for_the_calendar_period_and_degrades_to_none():
    state = ProgressState.__new__(ProgressState)
    days = [_row(TODAY, "iwe")]
    with patch("db.queries.users.moscow_today", return_value=TODAY), \
         patch.object(gateway_mcp, "dt_read_activity", AsyncMock(return_value={"days": days})) as read:
        assert await state._fetch_ecosystem_days(42, ACCOUNT) == days
    read.assert_awaited_once_with(42, "2026-10-15", "2026-11-12")

    with patch("db.queries.users.moscow_today", return_value=TODAY), \
         patch.object(gateway_mcp, "dt_read_activity", AsyncMock(return_value=None)):
        assert await state._fetch_ecosystem_days(42, ACCOUNT) is None
    with patch("db.queries.users.moscow_today", return_value=TODAY), \
         patch.object(gateway_mcp, "dt_read_activity", AsyncMock(side_effect=RuntimeError("down"))):
        assert await state._fetch_ecosystem_days(42, ACCOUNT) is None


# ─── Nightly all-time totals (3_derived.3_15_activity) ──────────────────────

def _totals(last_day: date, days: int, current: int, record: int) -> dict:
    return {"channels": {"all": {"active_days": days, "first_day": "2026-04-01", "last_day": last_day.isoformat(),
                                 "current_streak": current, "longest_streak": record, "record": record}}}


def _window_cache(today_active: bool) -> dict:
    cache = {"calendar": _calendar(29, bot_active=set(range(28)) | ({28} if today_active else set())),
             "streak": 3, "longest_streak": 5, "active_days_total": 10}
    apply_ecosystem(cache, [])
    return cache


def test_totals_lift_counters_above_the_window():
    cache = _window_cache(today_active=False)
    assert cache["streak"] == 28  # window cap before the nightly totals

    apply_totals(cache, _totals(TODAY - timedelta(days=1), days=180, current=45, record=60))

    assert (cache["streak"], cache["longest_streak"], cache["active_days_total"]) == (45, 60, 180)


def test_today_extends_a_run_the_night_did_not_see():
    cache = _window_cache(today_active=True)

    apply_totals(cache, _totals(TODAY - timedelta(days=1), days=180, current=60, record=60))

    assert (cache["streak"], cache["longest_streak"], cache["active_days_total"]) == (61, 61, 181)


def test_today_already_counted_by_the_night_is_not_added_twice():
    cache = _window_cache(today_active=True)

    apply_totals(cache, _totals(TODAY, days=181, current=61, record=61))

    assert (cache["streak"], cache["longest_streak"], cache["active_days_total"]) == (61, 61, 181)


def test_days_after_the_night_run_continue_its_streak():
    # night run of D-1 saw days up to D-2; D-1 is active in the calendar, today not yet (00:00-04:30)
    cache = {"calendar": _calendar(5, bot_active={3}), "streak": 1, "longest_streak": 1, "active_days_total": 1}

    apply_totals(cache, _totals(TODAY - timedelta(days=2), days=180, current=60, record=60))

    assert (cache["streak"], cache["longest_streak"], cache["active_days_total"]) == (61, 61, 181)


def test_night_totals_older_than_the_calendar_change_nothing():
    cache = {"calendar": _calendar(5), "streak": 2, "longest_streak": 3, "active_days_total": 4}

    apply_totals(cache, _totals(TODAY - timedelta(days=30), days=90, current=30, record=40))

    assert (cache["streak"], cache["longest_streak"], cache["active_days_total"]) == (2, 3, 4)


def test_stale_run_is_not_shown_as_current_but_record_stays():
    cache = {"calendar": _calendar(5), "streak": 0, "longest_streak": 2, "active_days_total": 3}

    apply_totals(cache, _totals(TODAY - timedelta(days=3), days=90, current=30, record=40))

    assert (cache["streak"], cache["longest_streak"], cache["active_days_total"]) == (0, 40, 90)


def test_totals_never_lower_bot_counters_and_ignore_missing_data():
    cache = {"calendar": _calendar(5), "streak": 7, "longest_streak": 99, "active_days_total": 500}
    apply_totals(cache, _totals(TODAY, days=12, current=2, record=9))
    assert (cache["streak"], cache["longest_streak"], cache["active_days_total"]) == (7, 99, 500)

    before = dict(cache)
    apply_totals(cache, {"channels": {}})
    assert cache == before


async def test_totals_read_uses_the_derived_path_best_effort():
    block = _totals(TODAY, days=1, current=1, record=1)
    result = {"content": [{"type": "text", "text": json.dumps(block)}]}
    with patch.object(gateway_mcp, "_call", AsyncMock(return_value=result)) as call:
        assert await gateway_mcp.dt_read_activity_totals(42) == block
    call.assert_awaited_once_with("dt_read_digital_twin", {"path": "3_derived.3_15_activity"}, 42,
                                  timeout=5, best_effort=True)

    with patch.object(gateway_mcp, "_call", AsyncMock(return_value=None)):
        assert await gateway_mcp.dt_read_activity_totals(42) is None


async def test_progress_totals_give_up_after_the_bound(monkeypatch):
    from states.utilities import progress

    async def hang(_chat_id):
        await asyncio.sleep(10)

    monkeypatch.setattr(progress, "OPTIONAL_GATEWAY_READ_SECONDS", 0.05)
    state = ProgressState.__new__(ProgressState)
    with patch.object(gateway_mcp, "dt_read_activity_totals", side_effect=hang):
        assert await state._fetch_activity_totals(42, ACCOUNT) is None


# ─── Gateway best-effort calls: no retry, shared breaker untouched ─────────

class _HangingPost:
    def __init__(self, calls):
        self.calls = calls

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs.get("timeout"))
        return self

    async def __aenter__(self):
        raise TimeoutError

    async def __aexit__(self, *exc):
        return False


async def _timeout_call(best_effort: bool) -> tuple[int, int]:
    calls: list = []
    session = type("S", (), {"post": _HangingPost(calls)})()
    with patch.object(gateway_mcp, "_get_session", AsyncMock(return_value=session)), \
         patch.object(gateway_mcp, "_get_access_token", return_value="token"), \
         patch("asyncio.sleep", AsyncMock()):
        gateway_mcp._failures, gateway_mcp._circuit_open = 0, False
        assert await gateway_mcp._do_call("dt_read_activity", {}, 42, timeout=5,
                                          best_effort=best_effort) is None
        return len(calls), gateway_mcp._failures


async def test_best_effort_timeout_is_one_attempt_and_not_a_breaker_failure():
    assert await _timeout_call(best_effort=True) == (1, 0)


async def test_regular_timeout_still_retries_and_counts():
    assert await _timeout_call(best_effort=False) == (2, 1)


# ─── Production hardening (review of the prod port, 13.11) ──────────────────

def test_day_masks_skip_malformed_rows_and_keep_the_good_ones():
    masks = day_masks([
        "not a row", None, {"channel": "iwe", "events": 3},
        {"day": TODAY.isoformat(), "channel": "club", "events": "many"},
        {"day": TODAY.isoformat(), "channel": "bot", "events": [1]},
        _row(TODAY, "iwe", 2),
    ])

    assert masks == {TODAY.isoformat(): CHANNEL_BITS["iwe"]}


def test_apply_with_malformed_rows_still_marks_the_good_days():
    cache = {"calendar": _calendar(3), "streak": 0, "longest_streak": 0, "active_days_total": 0}

    apply_ecosystem(cache, [None, {"day": TODAY.isoformat(), "events": "x", "channel": "iwe"},
                            _row(TODAY, "club")])

    assert cache["calendar"][-1]["mask"] == CHANNEL_BITS["club"]
    assert cache["calendar"][-1]["active"] is True


async def test_no_linked_account_means_no_gateway_call():
    state = ProgressState.__new__(ProgressState)
    with patch.object(gateway_mcp, "dt_read_activity", AsyncMock()) as days, \
         patch.object(gateway_mcp, "dt_read_activity_totals", AsyncMock()) as totals:
        assert await state._fetch_ecosystem_days(42, None) is None
        assert await state._fetch_activity_totals(42, "") is None
    days.assert_not_awaited()
    totals.assert_not_awaited()


class _OkResponse:
    status = 200

    async def json(self):
        return {"result": {"content": []}}


class _OkPost:
    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return _OkResponse()

    async def __aexit__(self, *exc):
        return False


async def _success_call(best_effort: bool) -> int:
    session = type("S", (), {"post": _OkPost()})()
    with patch.object(gateway_mcp, "_get_session", AsyncMock(return_value=session)), \
         patch.object(gateway_mcp, "_get_access_token", return_value="token"):
        gateway_mcp._failures, gateway_mcp._circuit_open = 2, False
        assert await gateway_mcp._do_call("dt_read_activity", {}, 42, timeout=5,
                                          best_effort=best_effort) == {"content": []}
        failures = gateway_mcp._failures
        gateway_mcp._failures = 0
        return failures


async def test_best_effort_success_does_not_reset_the_shared_breaker():
    assert await _success_call(best_effort=True) == 2


async def test_regular_success_still_resets_the_shared_breaker():
    assert await _success_call(best_effort=False) == 0
