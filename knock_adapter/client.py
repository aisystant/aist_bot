"""Single entry point for every outgoing call to Knock (WP-568 F17 item 5).

The adapter is stateless. The caller (the future notification center) owns the
"waiting for reconciliation" list: a Result with OUTCOME_UNKNOWN carries a
`reconcile_key`; `find_schedules` is the primitive to check what really exists.

UNVERIFIED API SHAPES: the paths, request bodies and response fields below follow
the trial report (18.11) only where it measured them; the rest is the writer's
understanding of the Knock API and MUST be confirmed at the live check:
- schedule create/list/delete shapes (`repeats`, `data`, `recipients`);
- that Knock returns a person's schedules together with their `data`;
- what `GET /users/{id}` returns while a bulk creation is still running.
Not implemented: one-off schedules, templates (deployed by a separate process
with the management key, which this adapter never receives).
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, time as dtime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .models import BatchResult, Outcome, Person, Result, RuleSpec
from .timecheck import (
    TimeCheckError,
    check_first_occurrence,
    check_minute,
    future_gap_warnings,
    load_zone,
)
from .transport import AdapterConfig, KnockTransport, Opener, default_opener, with_warnings

logger = logging.getLogger(__name__)


def _rejected(reason: str) -> Result:
    return Result(Outcome.REJECTED_BY_CHECK, reason)


def _quote(identifier: str) -> str:
    return urllib.parse.quote(identifier, safe="")


def _parse_schedule(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = item.get("data") or {}
    repeats = item.get("repeats") or []
    if not data.get("rule_id") or not repeats:
        return None
    return {
        "schedule_id": item.get("id"),
        "rule_id": data["rule_id"],
        "hour": repeats[0].get("hours"),
        "minute": repeats[0].get("minutes"),
        "place": data.get("place", ""),
    }


class KnockAdapter:
    def __init__(
        self,
        api_key: str,
        config: Optional[AdapterConfig] = None,
        *,
        opener: Opener = default_opener,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        rng=None,
    ) -> None:
        self._cfg = config or AdapterConfig()
        self._transport = KnockTransport(api_key, self._cfg, opener=opener, sleep=sleep, clock=clock, rng=rng)
        self._sleep = sleep
        self._clock = clock
        self._now = now

    # --- people ---------------------------------------------------------

    def ensure_person(self, person: Person) -> Result:
        try:
            load_zone(person.timezone)
        except TimeCheckError as error:
            return _rejected(str(error))
        if not person.person_id:
            return _rejected("person_id_missing")
        return self._transport.request(
            "PUT", "/users/" + _quote(person.person_id), {"timezone": person.timezone},
            safe_to_retry=self._cfg.person_upsert_idempotent,
            reconcile_key=("ensure_person", person.person_id, ""),
        )

    def get_person(self, person_id: str) -> Result:
        return self._transport.request("GET", "/users/" + _quote(person_id), safe_to_retry=True)

    def delete_person(self, person_id: str) -> Result:
        # "not found" means the wanted state (absent) is reached. The caller must
        # still re-check later if it holds an unknown-outcome create for this person.
        return self._transport.request(
            "DELETE", "/users/" + _quote(person_id), safe_to_retry=True, not_found_ok=True)

    def upsert_people(self, people: Sequence[Person]) -> BatchResult:
        """Bulk create; the provider runs it in the background (ACCEPTED_ASYNC)."""
        items: Dict[str, Result] = {}
        valid: List[Person] = []
        for person in people:
            try:
                if not person.person_id:
                    raise TimeCheckError("person_id_missing")
                load_zone(person.timezone)
            except TimeCheckError as error:
                items[person.person_id] = _rejected(str(error))
                continue
            valid.append(person)
        size = self._cfg.bulk_chunk_size
        chunks = [valid[i:i + size] for i in range(0, len(valid), size)]
        with ThreadPoolExecutor(max_workers=self._cfg.bulk_concurrency) as pool:
            for chunk, result in zip(chunks, pool.map(self._upsert_chunk, chunks)):
                for person in chunk:
                    # Each person gets its own reconcile key, not the key of the chunk's first person.
                    key = ("upsert_people", person.person_id, "") if result.reconcile_key else None
                    items[person.person_id] = replace(result, reconcile_key=key)
        return BatchResult(items)

    def _upsert_chunk(self, chunk: Sequence[Person]) -> Result:
        body = {"users": [{"id": p.person_id, "timezone": p.timezone} for p in chunk]}
        try:
            result = self._transport.request(
                "POST", "/users/bulk/identify", body, safe_to_retry=self._cfg.person_upsert_idempotent,
                batch=True, reconcile_key=("upsert_people", chunk[0].person_id, ""))
        except Exception as error:  # thread boundary: one bad chunk must not lose the other results
            logger.exception("[knock_adapter] bulk chunk crashed")
            return Result(Outcome.PERMANENT_ERROR, "adapter_internal_error:" + type(error).__name__)
        if result.outcome is Outcome.OK:
            return replace(result, outcome=Outcome.ACCEPTED_ASYNC)
        return result

    def wait_people_ready(self, person_ids: Iterable[str], budget: Optional[float] = None) -> Dict[str, bool]:
        """Poll until every person exists with a timezone. Schedules must not be
        created for people that are not ready (required of the caller)."""
        limit = self._cfg.total_budget if budget is None else budget
        started = self._clock()
        ready = {pid: False for pid in person_ids}
        while True:
            for pid, done in ready.items():
                if not done:
                    result = self.get_person(pid)
                    ready[pid] = result.ok and bool((result.data or {}).get("timezone"))
            if all(ready.values()) or self._clock() - started + self._cfg.poll_interval >= limit:
                return ready
            self._sleep(self._cfg.poll_interval)

    # --- schedules ------------------------------------------------------

    def create_schedule(self, person: Person, rule: RuleSpec) -> Result:
        try:
            zone = load_zone(person.timezone)
            if not person.person_id or not rule.rule_id:
                raise TimeCheckError("person_id_or_rule_id_missing")
            if rule.local_time.tzinfo is not None:
                raise TimeCheckError("local_time_must_be_naive")  # the zone comes from the person
            if rule.local_time.second or rule.local_time.microsecond:
                raise TimeCheckError("seconds_not_supported")
            check_minute(rule.local_time.minute)
            now = self._now()
            check_first_occurrence(zone, rule.local_time, now)
        except TimeCheckError as error:
            return _rejected(str(error))
        warnings = future_gap_warnings(zone, rule.local_time, now, self._cfg.future_gap_horizon_days)
        body = {
            "workflow": rule.workflow,
            "recipients": [person.person_id],
            "repeats": [{"frequency": "daily", "interval": 1,
                         "hours": rule.local_time.hour, "minutes": rule.local_time.minute}],
            # Our rule number travels in the schedule so that reconciliation can find it.
            "data": {"rule_id": rule.rule_id, "place": rule.place},
        }
        # A create that timed out is NOT repeated: it could duplicate the schedule.
        result = self._transport.request(
            "POST", "/schedules", body, safe_to_retry=False,
            reconcile_key=("create_schedule", person.person_id, rule.rule_id))
        return with_warnings(result, warnings)

    def find_schedules(self, person_id: str, rule_id: Optional[str] = None) -> Result:
        query = urllib.parse.urlencode([("recipients[]", person_id)])
        result = self._transport.request("GET", "/schedules?" + query, safe_to_retry=True)
        if not result.ok:
            return result
        parsed = [p for p in (_parse_schedule(i) for i in (result.data or {}).get("items", [])) if p]
        if rule_id is not None:
            parsed = [p for p in parsed if p["rule_id"] == rule_id]
        return replace(result, data={"schedules": parsed})

    def delete_schedule(self, schedule_id: str) -> Result:
        return self._transport.request(
            "DELETE", "/schedules", {"schedule_ids": [schedule_id]}, safe_to_retry=True, not_found_ok=True)

    def change_timezone(self, person_id: str, new_timezone: str) -> Result:
        """Dry run over the person's schedules first; nothing is written if one of
        them would start inside a gap of the new zone."""
        try:
            zone = load_zone(new_timezone)
        except TimeCheckError as error:
            return _rejected(str(error))
        listing = self.find_schedules(person_id)
        if not listing.ok:
            return replace(listing, reason="cannot_list_schedules:" + listing.reason)
        now = self._now()
        unsafe, warnings = [], []
        for schedule in listing.data["schedules"]:
            if not isinstance(schedule["hour"], int) or not isinstance(schedule["minute"], int):
                # Cannot prove it safe: refuse instead of skipping it silently.
                return _rejected("schedule_unreadable:" + str(schedule["rule_id"]))
            local = dtime(schedule["hour"], schedule["minute"])
            try:
                check_first_occurrence(zone, local, now)
            except TimeCheckError:
                unsafe.append(schedule["rule_id"])
                continue
            warnings.extend(future_gap_warnings(zone, local, now, self._cfg.future_gap_horizon_days))
        if unsafe:
            return _rejected("schedules_unsafe_in_new_timezone:" + ",".join(sorted(unsafe)))
        result = self.ensure_person(Person(person_id, new_timezone))
        note = ("existing_schedules_not_recreated (what Knock does with them: live check)",)
        return with_warnings(result, tuple(warnings) + note)

    # --- scenarios and export ------------------------------------------

    def trigger_workflow(
        self, workflow: str, person_id: str, data: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Result:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        safe = bool(idempotency_key) and self._cfg.trigger_idempotency_verified
        return self._transport.request(
            "POST", "/workflows/{}/trigger".format(_quote(workflow)),
            {"recipients": [person_id], "data": data or {}},
            safe_to_retry=safe, headers=headers,
            reconcile_key=("trigger_workflow", person_id, workflow))

    def export_rows(self, person_ids: Iterable[str]) -> Dict[str, Any]:
        """Neutral rows for a move to another core: one row per (person, rule)."""
        rows: List[Dict[str, Any]] = []
        failed: Dict[str, Result] = {}
        for pid in person_ids:
            person = self.get_person(pid)
            listing = self.find_schedules(pid) if person.ok else person
            if not listing.ok:
                failed[pid] = listing
                continue
            zone_name = (person.data or {}).get("timezone", "")
            for s in listing.data["schedules"]:
                rows.append({"person_id": pid, "rule_id": s["rule_id"], "hour": s["hour"],
                             "minute": s["minute"], "timezone": zone_name, "place": s["place"]})
        return {"rows": rows, "failed": failed}
