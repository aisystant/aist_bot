"""Test doubles for the Knock adapter: a scripted opener, a fake clock, an in-memory fake Knock."""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = "https://api.knock.app/v1"


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def json_response(status: int = 200, body: Any = None, headers: Optional[Dict[str, str]] = None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return status, headers or {}, raw


class ScriptedOpener:
    """Returns prepared steps in order; an Exception step is raised. Time cost is simulated."""

    def __init__(self, clock: FakeClock, steps: List[Any]) -> None:
        self.clock = clock
        self.steps = list(steps)
        self.calls: List[Tuple[str, str, float, Dict[str, str]]] = []

    def __call__(self, request, timeout: float):
        self.calls.append((request.method, request.full_url, timeout, dict(request.header_items())))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            self.clock.advance(timeout if isinstance(step, TimeoutError) else 0.1)
            raise step
        self.clock.advance(0.2)
        return step


class FakeKnock:
    """In-memory Knock with failure injection. Shapes follow the adapter's UNVERIFIED assumptions."""

    def __init__(self) -> None:
        self.users: Dict[str, Dict[str, Any]] = {}
        self.schedules: List[Dict[str, Any]] = []
        self.requests: List[Tuple[str, str, Any, Dict[str, str]]] = []
        self.hang_after_create = False   # store the schedule, then time out
        self.fail_when_body_has: Dict[str, int] = {}   # substring -> status
        self.not_ready_polls: Dict[str, int] = {}      # person id -> 404 answers left
        self.trigger_script: List[int] = []            # statuses for trigger calls, in order

    def __call__(self, request, timeout: float):
        path = request.full_url[len(BASE):]
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.method, path, body, dict(request.header_items())))
        for needle, status in self.fail_when_body_has.items():
            if needle in (request.data or b"").decode("utf-8"):
                return json_response(status, {"error": "injected"})
        route, _, query = path.partition("?")
        return self._route(request.method, route, query, body)

    def _route(self, method: str, route: str, query: str, body: Any):
        parts = [urllib.parse.unquote(p) for p in route.strip("/").split("/")]
        if parts[0] == "users" and len(parts) == 3 and parts[1] == "bulk":
            for user in body["users"]:
                self.users[user["id"]] = {"id": user["id"], "timezone": user["timezone"]}
            return json_response(200, {})
        if parts[0] == "users" and len(parts) == 2:
            return self._user(method, parts[1], body)
        if parts[0] == "schedules":
            return self._schedule(method, query, body)
        if parts[0] == "workflows":
            status = self.trigger_script.pop(0) if self.trigger_script else 200
            return json_response(status, {"ok": status == 200})
        return json_response(404, {})

    def _user(self, method: str, person_id: str, body: Any):
        if method == "PUT":
            self.users[person_id] = {"id": person_id, "timezone": body["timezone"]}
            return json_response(200, self.users[person_id])
        left = self.not_ready_polls.get(person_id, 0)
        if method == "GET" and left:
            self.not_ready_polls[person_id] = left - 1
            return json_response(404, {})
        if person_id not in self.users:
            return json_response(404, {})
        if method == "DELETE":
            del self.users[person_id]
            return json_response(200, {})
        return json_response(200, self.users[person_id])

    def _schedule(self, method: str, query: str, body: Any):
        if method == "POST":
            self.schedules.append({"id": "s{}".format(len(self.schedules) + 1), **{
                k: body[k] for k in ("workflow", "recipients", "repeats", "data")}})
            if self.hang_after_create:
                raise TimeoutError("timed out after the schedule was stored")
            return json_response(200, {"items": [self.schedules[-1]]})
        if method == "GET":
            person_id = urllib.parse.parse_qs(query)["recipients[]"][0]
            return json_response(200, {"items": [s for s in self.schedules if person_id in s["recipients"]]})
        wanted = set(body["schedule_ids"])
        found = [s for s in self.schedules if s["id"] in wanted]
        self.schedules = [s for s in self.schedules if s["id"] not in wanted]
        return json_response(200 if found else 404, {})

    def add_schedule(self, person_id: str, rule_id: str, hour: int, minute: int, place: str = "") -> None:
        self.schedules.append({
            "id": "s{}".format(len(self.schedules) + 1), "workflow": "wf", "recipients": [person_id],
            "repeats": [{"frequency": "daily", "hours": hour, "minutes": minute}],
            "data": {"rule_id": rule_id, "place": place}})
