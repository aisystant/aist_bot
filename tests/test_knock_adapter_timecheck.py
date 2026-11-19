"""Time checks of the Knock adapter (WP-568 F17 item 5): DST gaps, minute grid, zones."""
from datetime import date, datetime, time

import pytest

from knock_adapter.timecheck import (
    TimeCheckError,
    check_first_occurrence,
    check_minute,
    future_gap_dates,
    future_gap_warnings,
    load_zone,
    local_status,
)
from tests.knock_adapter_fakes import utc


@pytest.mark.parametrize("zone_name, naive, expected", [
    # Nicosia: spring 2027-03-28, 03:00 -> 04:00 (hangs at the provider, measured 18.11)
    ("Asia/Nicosia", datetime(2027, 3, 28, 3, 0), "gap"),
    ("Asia/Nicosia", datetime(2027, 3, 28, 3, 30), "gap"),
    ("Asia/Nicosia", datetime(2027, 3, 28, 3, 59), "gap"),
    ("Asia/Nicosia", datetime(2027, 3, 28, 2, 59), "ok"),
    ("Asia/Nicosia", datetime(2027, 3, 28, 4, 0), "ok"),
    ("Asia/Nicosia", datetime(2027, 3, 29, 3, 30), "ok"),      # next day works
    ("Asia/Nicosia", datetime(2027, 10, 31, 3, 30), "ambiguous"),  # autumn repeated hour
    ("America/New_York", datetime(2027, 3, 14, 2, 30), "gap"),
    ("America/New_York", datetime(2027, 11, 7, 1, 30), "ambiguous"),
    ("Asia/Beirut", datetime(2027, 3, 28, 0, 30), "gap"),      # gap right after midnight
    ("Australia/Lord_Howe", datetime(2027, 10, 3, 2, 15), "gap"),  # 30-minute shift
])
def test_local_status(zone_name, naive, expected):
    assert local_status(load_zone(zone_name), naive) == expected


def test_autumn_ambiguous_is_not_a_gap_and_passes_first_occurrence_check():
    zone = load_zone("Asia/Nicosia")
    now = utc(2027, 10, 30, 12, 0)
    assert check_first_occurrence(zone, time(3, 30), now) == date(2027, 10, 31)


@pytest.mark.parametrize("minute", [0, 5, 30, 55])
def test_minute_on_grid_is_accepted(minute):
    check_minute(minute)


@pytest.mark.parametrize("minute, nearest", [(7, "nearest 5 or 10"), (58, "nearest 55 or 0"), (1, "nearest 0 or 5")])
def test_minute_off_grid_is_rejected_with_nearest_values(minute, nearest):
    with pytest.raises(TimeCheckError) as error:
        check_minute(minute)
    assert "minute_not_supported_for_recurring:{}".format(minute) in str(error.value)
    assert nearest in str(error.value)


@pytest.mark.parametrize("name, reason", [("", "timezone_missing"), (None, "timezone_missing"),
                                          ("Mars/Base", "timezone_unknown:Mars/Base")])
def test_bad_zone_names_are_refused(name, reason):
    with pytest.raises(TimeCheckError) as error:
        load_zone(name)
    assert str(error.value) == reason


def test_first_occurrence_in_gap_is_refused_with_the_date():
    zone = load_zone("Asia/Nicosia")
    now = utc(2027, 3, 27, 12, 0)  # 14:00 local on the day before the transition
    with pytest.raises(TimeCheckError) as error:
        check_first_occurrence(zone, time(3, 30), now)
    assert str(error.value) == "first_occurrence_in_dst_gap:2027-03-28 03:30"


def test_first_occurrence_rolls_to_tomorrow_when_today_has_passed():
    zone = load_zone("Asia/Nicosia")
    now = utc(2027, 1, 10, 12, 0)  # 14:00 local; 07:00 already passed today
    assert check_first_occurrence(zone, time(7, 0), now) == date(2027, 1, 11)
    assert check_first_occurrence(zone, time(15, 0), now) == date(2027, 1, 10)


def test_future_gap_dates_found_within_horizon_only():
    zone = load_zone("Asia/Nicosia")
    now = utc(2027, 1, 10, 12, 0)
    assert future_gap_dates(zone, time(3, 30), now, 730) == [date(2027, 3, 28), date(2028, 3, 26)]
    assert future_gap_dates(zone, time(3, 30), now, 100) == [date(2027, 3, 28)]
    assert future_gap_dates(zone, time(7, 0), now, 730) == []


def test_future_gap_warning_text_asks_for_live_check():
    warnings = future_gap_warnings(load_zone("Asia/Nicosia"), time(3, 30), utc(2027, 1, 10, 12, 0), 100)
    assert warnings == ("future_dst_gap:2027-03-28 (live check needed)",)
