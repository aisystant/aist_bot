"""Ecosystem activity folded into the /progress overview (WP-417 Ф7)."""

import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

from clients.gateway_mcp import gateway_mcp
from core.activity_board import CHANNEL_BITS, apply_ecosystem, day_masks
from integrations.telegram.widgets import build_progress_chat_data
from states.utilities.progress import ProgressState

TODAY = date(2026, 11, 12)  # Thursday


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
    call.assert_awaited_once_with("dt_read_activity", {"from": "2026-10-15", "to": "2026-11-12"}, 42, timeout=5)


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
        assert await state._fetch_ecosystem_days(42) == days
    read.assert_awaited_once_with(42, "2026-10-15", "2026-11-12")

    with patch("db.queries.users.moscow_today", return_value=TODAY), \
         patch.object(gateway_mcp, "dt_read_activity", AsyncMock(return_value=None)):
        assert await state._fetch_ecosystem_days(42) is None
    with patch("db.queries.users.moscow_today", return_value=TODAY), \
         patch.object(gateway_mcp, "dt_read_activity", AsyncMock(side_effect=RuntimeError("down"))):
        assert await state._fetch_ecosystem_days(42) is None
