"""Local-time safety checks (daylight-saving gaps, minute grid).

Measured on 2026-11-18 (trial report, WP-568 F17 item 5): a schedule whose start
falls into the spring "gap" (3:00 and 3:30 in Nicosia) hangs the request for more
than a minute. Not measured: other zones, times next to the gap, what Knock does
with a daily schedule that reaches a transition day later. Hence two rules:
- the FIRST occurrence in a gap is rejected (measured);
- a FUTURE occurrence in a gap is only a warning (not measured; the pilot decides
  after the live check).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import List, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Knock accepts only minutes divisible by 5 in repeating schedules (trial, 18.11).
MINUTE_STEP = 5


class TimeCheckError(ValueError):
    """Raised for input we refuse before sending anything."""


def load_zone(name: str) -> ZoneInfo:
    if not name or not isinstance(name, str):
        raise TimeCheckError("timezone_missing")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        # OSError: the image has no tz database; the caller must add `tzdata`.
        raise TimeCheckError("timezone_unknown:{}".format(name)) from exc


def check_minute(minute: int) -> None:
    if minute % MINUTE_STEP:
        lower = minute - minute % MINUTE_STEP
        upper = lower + MINUTE_STEP
        raise TimeCheckError(
            "minute_not_supported_for_recurring:{} (nearest {} or {})".format(
                minute, lower, upper % 60
            )
        )


def local_status(zone: ZoneInfo, naive: datetime) -> str:
    """Return 'ok', 'gap' (nonexistent local time) or 'ambiguous' (repeated hour).

    Explicit round trip: zoneinfo itself never raises for a nonexistent time.
    """
    first = naive.replace(tzinfo=zone, fold=0)
    back = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if back != naive:
        return "gap"
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        return "ambiguous"
    return "ok"


def first_occurrence_date(zone: ZoneInfo, local_time: time, now_utc: datetime) -> date:
    """Date of the next daily occurrence strictly after now (in the person's zone)."""
    now_local = now_utc.astimezone(zone)
    candidate = now_local.date()
    if datetime.combine(candidate, local_time) <= now_local.replace(tzinfo=None):
        candidate += timedelta(days=1)
    return candidate


def check_first_occurrence(zone: ZoneInfo, local_time: time, now_utc: datetime) -> date:
    day = first_occurrence_date(zone, local_time, now_utc)
    if local_status(zone, datetime.combine(day, local_time)) == "gap":
        raise TimeCheckError(
            "first_occurrence_in_dst_gap:{} {}".format(day.isoformat(), local_time.strftime("%H:%M"))
        )
    return day


def future_gap_dates(
    zone: ZoneInfo, local_time: time, now_utc: datetime, horizon_days: int
) -> List[date]:
    """Days within the horizon on which the daily time does not exist locally."""
    start = now_utc.astimezone(zone).date()
    dates = []
    for offset in range(horizon_days + 1):
        day = start + timedelta(days=offset)
        if local_status(zone, datetime.combine(day, local_time)) == "gap":
            dates.append(day)
    return dates


def future_gap_warnings(
    zone: ZoneInfo, local_time: time, now_utc: datetime, horizon_days: int
) -> Tuple[str, ...]:
    dates = future_gap_dates(zone, local_time, now_utc, horizon_days)
    return tuple("future_dst_gap:{} (live check needed)".format(d.isoformat()) for d in dates)
