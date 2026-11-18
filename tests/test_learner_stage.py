"""Stage of the learner role on profile screens (WP-417 Ф8, pilot 18.11.2026, option 1A)."""

from datetime import date, datetime, timedelta, timezone

from core.learner_stage import learner_stage, stage_text

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
LEARNER = {"level": "Ученик", "numeric": 10}
REFORMER = {"level": "Реформатор", "numeric": 60}


def _derived(stage):
    return {"3_4_qualification": {"stage": stage}}


def _assessment(stage=3, days_ago=10, valid_until="2027-01-01T00:00:00+00:00"):
    return {"stage": stage, "assessed_at": (NOW - timedelta(days=days_ago)).isoformat(),
            "valid_until": valid_until}


def test_nightly_stage_comes_first_even_with_a_newer_self_assessment():
    assert learner_stage(_derived(2), LEARNER, _assessment(stage=4, days_ago=1), NOW) == {
        "stage": 2, "assessed_on": None}


def test_self_assessment_fills_an_empty_nightly_stage_with_its_moscow_date():
    late_evening_utc = {"stage": 3, "assessed_at": "2026-11-10T22:30:00+00:00", "valid_until": None}
    assert learner_stage({}, LEARNER, late_evening_utc, NOW) == {"stage": 3, "assessed_on": date(2026, 11, 11)}


def test_self_assessment_without_term_counts_only_180_days():
    assert learner_stage({}, None, _assessment(days_ago=179, valid_until=None), NOW)["stage"] == 3
    assert learner_stage({}, None, _assessment(days_ago=181, valid_until=None), NOW) is None


def test_no_stage_for_qualified_people_and_without_sources():
    assert learner_stage(_derived(2), REFORMER, _assessment(), NOW) is None
    assert learner_stage({}, LEARNER, None, NOW) is None
    assert learner_stage({}, LEARNER, {"stage": 3, "assessed_at": "not a date"}, NOW) is None


def test_stage_zero_from_the_nightly_run_is_a_stage_not_a_gap():
    assert learner_stage(_derived(0), LEARNER, _assessment(), NOW) == {"stage": 0, "assessed_on": None}


def test_stage_text_marks_the_self_assessment():
    assert stage_text({"stage": 3, "assessed_on": None}, "ru") == "Систематический (3/5)"
    assert stage_text({"stage": 3, "assessed_on": date(2026, 11, 11)}, "ru") == (
        "Систематический (3/5) — самооценка по диагностике от 11.11.2026")
    assert stage_text({"stage": 2, "assessed_on": date(2026, 11, 11)}, "en") == (
        "Practicing (2/5) — self-assessment from the diagnostic on 11.11.2026")
