"""Activity board across the ecosystem (WP-417 Ф7).

The twin server tool ``read_activity`` returns days x places (iwe, club, lms, bot) from the
platform event journal. This module folds that answer into the /progress overview cache:
the calendar shows which places each day came from, and the counters never go below what the
bot already knew. Streak, record and all-time totals over the whole history are the
Profiler's job (3_derived.3_15_activity, nightly); ``apply_totals`` folds them in, and without
them the numbers stay lower bounds taken from the 4-week window.
"""

from __future__ import annotations

from datetime import date, timedelta

# Bit per place; the widget colours a calendar cell by the bits set in its mask.
CHANNEL_BITS: dict[str, int] = {"iwe": 1, "club": 2, "lms": 4, "bot": 8}


def day_masks(days: list[dict]) -> dict[str, int]:
    """ISO date -> bit mask of places active that day. Unknown places ('other') never count.

    Malformed rows (not a dict, no day, non-numeric events) are skipped, so one bad row from
    the server cannot turn /progress into an error screen.
    """
    masks: dict[str, int] = {}
    for row in days:
        if not isinstance(row, dict) or not isinstance(row.get("day"), str):
            continue
        bit = CHANNEL_BITS.get(row.get("channel"))
        try:
            events = int(row.get("events") or 0)
        except (TypeError, ValueError):
            continue
        if bit and events > 0:
            masks[row["day"]] = masks.get(row["day"], 0) | bit
    return masks


def _runs(active: list[bool]) -> tuple[int, int]:
    """(current streak, longest streak). Today still open: a run ending yesterday stays current."""
    longest = run = 0
    for flag in active:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    tail = active[:-1] if active and not active[-1] else active
    current = 0
    for flag in reversed(tail):
        if not flag:
            break
        current += 1
    return current, longest


def apply_ecosystem(cache: dict, days: list[dict]) -> None:
    """Fold the ecosystem answer into the overview cache in place.

    A day is active when the bot or any place says so. Counters take the larger of the bot's
    own value and the window value, so the numbers people already have never drop.
    """
    calendar = cache.get("calendar") or []
    if not calendar:
        return
    masks = day_masks(days)
    for entry in calendar:
        mask = masks.get(entry["date"], 0)
        entry["mask"] = mask
        entry["active"] = bool(entry.get("active")) or mask > 0

    active = [bool(entry["active"]) for entry in calendar]
    current, longest = _runs(active)
    today = date.fromisoformat(calendar[-1]["date"])
    monday = today - timedelta(days=today.weekday())
    cache["days_active_week"] = sum(1 for e in calendar if e["active"] and date.fromisoformat(e["date"]) >= monday)
    cache["streak"] = max(int(cache.get("streak") or 0), current)
    cache["longest_streak"] = max(int(cache.get("longest_streak") or 0), longest)
    cache["active_days_total"] = max(int(cache.get("active_days_total") or 0), sum(active))
    cache["calendar_active_count"] = sum(active)
    cache["ecosystem"] = True


def apply_totals(cache: dict, totals: dict) -> None:
    """Lift streak, record and all-time days with the Profiler's nightly totals (3_15_activity).

    The nightly values stop at their last_day; the calendar (already folded with the ecosystem
    days) covers the days after it up to today. Those later days are added to the total, and
    the nightly run continues while every one of them is active (today may still be empty).
    Counters still never go below what the bot or the window know.
    """
    calendar = cache.get("calendar") or []
    overall = (totals.get("channels") or {}).get("all")
    if not calendar or not isinstance(overall, dict):
        return
    last_day = date.fromisoformat(overall["last_day"])
    later = [bool(e["active"]) for e in calendar if date.fromisoformat(e["date"]) > last_day]
    if len(later) > len(calendar) - 1:
        return  # night totals older than the whole calendar: the gap cannot be checked

    gap = later[:-1] if later and not later[-1] else later  # today may still be empty
    streak = int(overall.get("current_streak") or 0) + sum(later) if all(gap) else 0
    days_total = int(overall.get("active_days") or 0) + sum(later)
    record = max(int(overall.get("record") or 0), streak)

    cache["streak"] = max(int(cache.get("streak") or 0), streak)
    cache["longest_streak"] = max(int(cache.get("longest_streak") or 0), record, cache["streak"])
    cache["active_days_total"] = max(int(cache.get("active_days_total") or 0), days_total)
