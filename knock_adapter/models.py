"""Domain types of the Knock adapter.

No Knock-specific types leak out of this package: callers see Person, RuleSpec,
Result and plain dict rows only (WP-568 F17 item 5, condition "one adapter").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class Outcome(str, Enum):
    OK = "ok"
    # Bulk call accepted, executes in the background at the provider.
    ACCEPTED_ASYNC = "accepted_async"
    # Rejected before processing (rate limit) or a safe-to-repeat call that kept failing.
    RETRYABLE_ERROR = "retryable_error"
    PERMANENT_ERROR = "permanent_error"
    # A safe-to-repeat call ran out of time budget.
    TIMEOUT = "timeout"
    # A mutating call whose effect is unknown (timeout, 5xx, connection cut).
    # Never repeated blindly; the caller reconciles via `reconcile_key`.
    OUTCOME_UNKNOWN = "outcome_unknown"
    # Refused by our own validation before any request was sent.
    REJECTED_BY_CHECK = "rejected_by_check"


@dataclass(frozen=True)
class Person:
    person_id: str  # our identifier, never a Telegram id
    timezone: str   # IANA name; Knock takes the zone from the person record


@dataclass(frozen=True)
class RuleSpec:
    """Daily recurring rule. One-off schedules are not supported yet."""
    rule_id: str    # our rule number; stored in the schedule data for reconciliation
    workflow: str
    local_time: time
    place: str = ""


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    reason: str = ""
    status_code: Optional[int] = None
    data: Optional[Dict[str, Any]] = None
    attempts: int = 0
    warnings: Tuple[str, ...] = ()
    # (operation, person_id, rule_id) for the caller's reconciliation queue.
    reconcile_key: Optional[Tuple[str, str, str]] = None

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.OK, Outcome.ACCEPTED_ASYNC)


@dataclass(frozen=True)
class BatchResult:
    """Per-item outcomes; a chunk failure marks exactly the items of that chunk."""
    items: Dict[str, Result] = field(default_factory=dict)

    @property
    def all_ok(self) -> bool:
        return bool(self.items) and all(r.ok for r in self.items.values())
