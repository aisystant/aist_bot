"""Domain facade of the Knock adapter, against an in-memory fake Knock."""
from datetime import time

import pytest

from knock_adapter import AdapterConfig, KnockAdapter, Outcome, Person, RuleSpec
from tests.knock_adapter_fakes import FakeClock, FakeKnock, ScriptedOpener, json_response, utc

NICOSIA = "Asia/Nicosia"


def make(fake=None, now=utc(2027, 1, 10, 12, 0), **config):
    clock = FakeClock()
    fake = fake or FakeKnock()
    adapter = KnockAdapter("key", AdapterConfig(**config), opener=fake, sleep=clock.sleep, clock=clock,
                           now=lambda: now)
    return adapter, fake, clock


def rule(hour=7, minute=0, rule_id="r1"):
    return RuleSpec(rule_id=rule_id, workflow="daily-weather", local_time=time(hour, minute), place="Nicosia")


def test_create_schedule_sends_zone_free_daily_repeat_with_our_rule_number():
    adapter, fake, _ = make()
    result = adapter.create_schedule(Person("p1", NICOSIA), rule())
    assert result.outcome is Outcome.OK and result.warnings == ()
    assert fake.schedules[0]["repeats"] == [{"frequency": "daily", "interval": 1, "hours": 7, "minutes": 0}]
    assert fake.schedules[0]["data"] == {"rule_id": "r1", "place": "Nicosia"}
    assert fake.schedules[0]["recipients"] == ["p1"]


@pytest.mark.parametrize("person, spec, reason", [
    (Person("p1", NICOSIA), rule(minute=7), "minute_not_supported_for_recurring:7"),
    (Person("p1", "Mars/Base"), rule(), "timezone_unknown:Mars/Base"),
    (Person("p1", ""), rule(), "timezone_missing"),
    (Person("", NICOSIA), rule(), "person_id_or_rule_id_missing"),
])
def test_invalid_input_is_refused_before_any_request(person, spec, reason):
    adapter, fake, _ = make()
    result = adapter.create_schedule(person, spec)
    assert result.outcome is Outcome.REJECTED_BY_CHECK and result.reason.startswith(reason)
    assert fake.requests == []


def test_time_with_its_own_zone_is_refused_because_the_zone_comes_from_the_person():
    adapter, fake, _ = make()
    aware = RuleSpec("r1", "wf", time(7, 0, tzinfo=utc(2027, 1, 1).tzinfo))
    result = adapter.create_schedule(Person("p1", NICOSIA), aware)
    assert (result.outcome, result.reason) == (Outcome.REJECTED_BY_CHECK, "local_time_must_be_naive")
    assert fake.requests == []


def test_first_occurrence_inside_the_dst_gap_is_refused_without_a_request():
    adapter, fake, _ = make(now=utc(2027, 3, 27, 12, 0))
    result = adapter.create_schedule(Person("p1", NICOSIA), rule(3, 30))
    assert (result.outcome, result.reason) == (
        Outcome.REJECTED_BY_CHECK, "first_occurrence_in_dst_gap:2027-03-28 03:30")
    assert fake.requests == []


def test_future_gap_is_only_a_warning_and_the_schedule_is_created():
    adapter, fake, _ = make()  # first occurrence 2027-01-11 03:30 exists
    result = adapter.create_schedule(Person("p1", NICOSIA), rule(3, 30))
    assert result.outcome is Outcome.OK
    assert result.warnings[0] == "future_dst_gap:2027-03-28 (live check needed)"
    assert len(fake.schedules) == 1


def test_create_that_dies_after_storing_is_not_repeated_and_can_be_reconciled():
    fake = FakeKnock()
    fake.hang_after_create = True
    adapter, _, clock = make(fake)
    result = adapter.create_schedule(Person("p1", NICOSIA), rule())
    assert result.outcome is Outcome.OUTCOME_UNKNOWN
    assert result.reconcile_key == ("create_schedule", "p1", "r1")
    assert len([r for r in fake.requests if r[0] == "POST"]) == 1
    assert clock.sleeps == []
    found = adapter.find_schedules("p1", "r1")
    assert [s["rule_id"] for s in found.data["schedules"]] == ["r1"]


def test_find_schedules_filters_by_rule_and_skips_foreign_schedules():
    adapter, fake, _ = make()
    fake.add_schedule("p1", "r1", 7, 0, "A")
    fake.add_schedule("p1", "r2", 9, 30, "B")
    fake.schedules.append({"id": "x", "workflow": "w", "recipients": ["p1"], "repeats": [], "data": {}})
    both = adapter.find_schedules("p1").data["schedules"]
    only = adapter.find_schedules("p1", "r2").data["schedules"]
    assert [s["rule_id"] for s in both] == ["r1", "r2"]
    assert only == [{"schedule_id": "s2", "rule_id": "r2", "hour": 9, "minute": 30, "place": "B"}]


def test_delete_person_twice_is_success_both_times():
    adapter, fake, _ = make()
    adapter.ensure_person(Person("p1", NICOSIA))
    first, second = adapter.delete_person("p1"), adapter.delete_person("p1")
    assert (first.outcome, first.reason) == (Outcome.OK, "")
    assert (second.outcome, second.reason) == (Outcome.OK, "already_absent")
    assert fake.users == {}


def test_person_id_cannot_escape_the_path():
    adapter, fake, _ = make()
    adapter.get_person("../schedules")
    assert fake.requests[0][1] == "/users/..%2Fschedules"


def test_bulk_people_are_chunked_and_failure_is_reported_per_item():
    adapter, fake, _ = make()
    fake.fail_when_body_has = {'"id": "u100"': 400}  # the first person of the second chunk
    people = [Person("u{}".format(i), NICOSIA) for i in range(250)] + [Person("bad", "Mars/Base")]
    batch = adapter.upsert_people(people)
    bulk_calls = [r for r in fake.requests if r[1] == "/users/bulk/identify"]
    assert sorted(len(r[2]["users"]) for r in bulk_calls) == [50, 100, 100]
    assert batch.items["u0"].outcome is Outcome.ACCEPTED_ASYNC
    assert batch.items["u249"].outcome is Outcome.ACCEPTED_ASYNC
    assert batch.items["u150"].outcome is Outcome.PERMANENT_ERROR and batch.items["u100"].status_code == 400
    assert batch.items["bad"].outcome is Outcome.REJECTED_BY_CHECK
    assert not batch.all_ok


def test_bulk_unknown_outcome_gives_every_person_their_own_reconcile_key():
    adapter, fake, _ = make(person_upsert_idempotent=False)
    fake.fail_when_body_has = {'"id": "a"': 503}
    batch = adapter.upsert_people([Person("a", NICOSIA), Person("b", NICOSIA)])
    assert {pid: r.outcome for pid, r in batch.items.items()} == {
        "a": Outcome.OUTCOME_UNKNOWN, "b": Outcome.OUTCOME_UNKNOWN}
    assert batch.items["a"].reconcile_key == ("upsert_people", "a", "")
    assert batch.items["b"].reconcile_key == ("upsert_people", "b", "")


def test_a_crashing_bulk_chunk_does_not_lose_the_other_chunks():
    adapter, _, _ = make()
    original = adapter._transport.request

    def crashing(method, path, body=None, **kwargs):
        if body and any(u["id"] == "u100" for u in body.get("users", [])):
            raise RuntimeError("boom")
        return original(method, path, body, **kwargs)

    adapter._transport.request = crashing
    batch = adapter.upsert_people([Person("u{}".format(i), NICOSIA) for i in range(150)])
    assert batch.items["u0"].outcome is Outcome.ACCEPTED_ASYNC
    assert (batch.items["u120"].outcome, batch.items["u120"].reason) == (
        Outcome.PERMANENT_ERROR, "adapter_internal_error:RuntimeError")
    assert len(batch.items) == 150


def test_bulk_refuses_a_person_without_an_id():
    adapter, fake, _ = make()
    batch = adapter.upsert_people([Person("", NICOSIA)])
    assert batch.items[""].reason == "person_id_missing" and fake.requests == []


def test_wait_people_ready_polls_until_the_person_appears():
    fake = FakeKnock()
    fake.users["p1"] = {"id": "p1", "timezone": NICOSIA}
    fake.not_ready_polls["p1"] = 2
    adapter, _, clock = make(fake)
    assert adapter.wait_people_ready(["p1"]) == {"p1": True}
    assert clock.sleeps == [2.0, 2.0]


def test_wait_people_ready_gives_up_within_the_budget():
    adapter, _, clock = make()
    assert adapter.wait_people_ready(["ghost"], budget=5.0) == {"ghost": False}
    assert sum(clock.sleeps) < 5.0


def test_change_timezone_refuses_when_an_existing_schedule_would_start_in_the_new_gap():
    fake = FakeKnock()
    fake.add_schedule("p1", "r1", 3, 30)
    adapter, _, _ = make(fake, now=utc(2027, 3, 27, 12, 0))
    result = adapter.change_timezone("p1", NICOSIA)
    assert (result.outcome, result.reason) == (
        Outcome.REJECTED_BY_CHECK, "schedules_unsafe_in_new_timezone:r1")
    assert [r for r in fake.requests if r[0] == "PUT"] == []


def test_change_timezone_writes_the_zone_and_warns_that_schedules_are_not_recreated():
    fake = FakeKnock()
    fake.add_schedule("p1", "r1", 7, 0)
    adapter, _, _ = make(fake)
    result = adapter.change_timezone("p1", "America/New_York")
    assert result.outcome is Outcome.OK
    assert fake.users["p1"]["timezone"] == "America/New_York"
    assert any(w.startswith("existing_schedules_not_recreated") for w in result.warnings)


def test_change_timezone_refuses_a_schedule_it_cannot_read_instead_of_skipping_it():
    fake = FakeKnock()
    fake.schedules.append({"id": "s1", "workflow": "w", "recipients": ["p1"],
                           "repeats": [{"frequency": "daily"}], "data": {"rule_id": "r9"}})
    adapter, _, _ = make(fake)
    result = adapter.change_timezone("p1", NICOSIA)
    assert (result.outcome, result.reason) == (Outcome.REJECTED_BY_CHECK, "schedule_unreadable:r9")
    assert [r for r in fake.requests if r[0] == "PUT"] == []


def test_change_timezone_does_not_write_when_the_schedules_cannot_be_listed():
    clock = FakeClock()
    opener = ScriptedOpener(clock, [json_response(422, {})])
    adapter = KnockAdapter("key", AdapterConfig(), opener=opener, sleep=clock.sleep, clock=clock,
                           now=lambda: utc(2027, 1, 10, 12, 0))
    result = adapter.change_timezone("p1", NICOSIA)
    assert result.outcome is Outcome.PERMANENT_ERROR and result.reason.startswith("cannot_list_schedules:")
    assert len(opener.calls) == 1


def test_trigger_without_verified_idempotency_is_never_repeated():
    fake = FakeKnock()
    fake.trigger_script = [503, 200]
    adapter, _, _ = make(fake)
    result = adapter.trigger_workflow("daily weather", "p1", {"x": 1}, idempotency_key="k1")
    assert result.outcome is Outcome.OUTCOME_UNKNOWN
    assert fake.requests[0][1] == "/workflows/daily%20weather/trigger"
    assert len(fake.requests) == 1


def test_trigger_with_verified_idempotency_key_is_repeated_with_the_header():
    fake = FakeKnock()
    fake.trigger_script = [503, 200]
    adapter, _, _ = make(fake, trigger_idempotency_verified=True)
    result = adapter.trigger_workflow("w", "p1", idempotency_key="k1")
    assert (result.outcome, result.attempts) == (Outcome.OK, 2)
    assert fake.requests[1][3]["Idempotency-key"] == "k1"


def test_export_rows_are_neutral_and_failures_are_listed_separately():
    fake = FakeKnock()
    fake.users["p1"] = {"id": "p1", "timezone": NICOSIA}
    fake.add_schedule("p1", "r1", 7, 0, "Nicosia")
    fake.add_schedule("p1", "r2", 21, 30, "Larnaca")
    adapter, _, _ = make(fake)
    exported = adapter.export_rows(["p1", "ghost"])
    assert exported["rows"] == [
        {"person_id": "p1", "rule_id": "r1", "hour": 7, "minute": 0, "timezone": NICOSIA, "place": "Nicosia"},
        {"person_id": "p1", "rule_id": "r2", "hour": 21, "minute": 30, "timezone": NICOSIA, "place": "Larnaca"},
    ]
    assert list(exported["failed"]) == ["ghost"] and exported["failed"]["ghost"].reason == "not_found"
