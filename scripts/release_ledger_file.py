"""Durable file-backed ReleaseLedgerPort (WP-562 Ф3.1).

Peer-session 2026-10-14-02-wp562-f3-1-ledger (Claude+Codex, Kimi unavailable
this session -- OAuth lineage cutover pending, unrelated to this change).

Design, agreed after Codex found two correctness holes in the first draft:
* The journal (one append-only JSON-lines file) is the ONLY source of truth
  for every CAS decision. There is no index or cache on the decision path --
  an index can go stale between an append and its own update, and nothing
  short of re-deriving state from the journal itself can prove a cached
  snapshot covers every record written so far.
* Every CAS method takes the file lock, replays the ENTIRE journal from the
  start to rebuild in-memory state, compares against the caller's expected
  state/version/fencing_token (exactly the semantics already codified in
  `FakeLedger`, tests/test_release_control.py:748+), appends one record on
  success, then releases the lock. A malformed record stops the read instead
  of being skipped -- same fail-closed rule as release-drift-detector.py.
* The fencing token is `max(every fencing_token in the journal) + 1`, so it
  can never drift from the journal it is derived from.

This trades per-call read cost (a full scan) for a correctness property that
a cache cannot give without its own CAS -- acceptable at the release volume
this ledger exists for (H-314: a handful of promotions a day, not a
high-throughput store).
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import pathlib
import uuid
from datetime import UTC, datetime
from typing import Any

from scripts.release_control import (
    AppliedReleaseReceipt,
    ClaimDisposition,
    ClaimReceipt,
    LedgerClaimRequest,
    LedgerOutcomeRequest,
    LedgerStateSnapshot,
    NegativeMatrixQuery,
    NegativeMatrixReceipt,
    OperationState,
    PilotReceiptQuery,
    ProviderIntentRecordRequest,
    ReconciliationCASRequest,
    ReleaseAction,
    TargetName,
)

DEFAULT_LEDGER_PATH = (
    pathlib.Path.home() / "IWE/DS-my-strategy/machine/release-ledger.jsonl"
)


class LedgerJournalError(RuntimeError):
    """The journal cannot be trusted; refuse the CAS instead of guessing."""


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _now() -> str:
    return _timestamp(datetime.now(UTC))


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise LedgerJournalError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise LedgerJournalError("timestamp must include a timezone")
    return parsed


@contextlib.contextmanager
def _journal_lock(lock_path: pathlib.Path):
    """Not reentrant: a second acquisition from the same process deadlocks.

    No public FileLedger method currently calls another public method from
    inside its own `with _journal_lock(...)` block -- keep it that way.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


_PILOT_RECEIPT_FIELDS = (
    "repository",
    "manifest_release_id",
    "operation_fingerprint",
    "artifact_digest",
    "embedded_manifest_hash",
    "contract_digest",
    "contract_edition",
    "required_checks_digest",
)

_NEGATIVE_MATRIX_FIELDS = (
    "evidence_ref",
    "repository",
    "contract_digest",
    "contract_edition",
    "pilot_source_ref",
    "production_source_ref",
)


@dataclasses.dataclass
class _ReplayedState:
    states: dict[str, LedgerStateSnapshot] = dataclasses.field(default_factory=dict)
    claims: dict[str, ClaimReceipt] = dataclasses.field(default_factory=dict)
    outcome_fields: dict[str, dict[str, Any]] = dataclasses.field(
        default_factory=dict
    )
    pilot_receipts: dict[tuple[str, ...], AppliedReleaseReceipt] = dataclasses.field(
        default_factory=dict
    )
    negative_matrix_receipts: list[NegativeMatrixReceipt] = dataclasses.field(
        default_factory=list
    )
    max_fencing_token: int = 0


class FileLedger:
    """Append-only, file-locked ReleaseLedgerPort (single host, see README note)."""

    def __init__(self, path: pathlib.Path = DEFAULT_LEDGER_PATH) -> None:
        self._path = path
        self._lock_path = path.with_suffix(path.suffix + ".lock")

    def _read_records(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerJournalError(
                        f"malformed journal record at line {line_number}: {exc}"
                    ) from exc
                if not isinstance(record, dict) or "kind" not in record:
                    raise LedgerJournalError(
                        f"journal record at line {line_number} is missing 'kind'"
                    )
                records.append(record)
        return records

    def _replay(self) -> _ReplayedState:
        """Rebuild every snapshot from the journal; the only path CAS trusts."""

        replayed = _ReplayedState()
        for record in self._read_records():
            kind = record["kind"]
            fencing = record.get("fencing_token")
            if isinstance(fencing, int):
                replayed.max_fencing_token = max(replayed.max_fencing_token, fencing)
            try:
                self._apply_record(replayed, kind, record)
            except (KeyError, ValueError) as exc:
                raise LedgerJournalError(
                    f"malformed {kind!r} record: {exc}"
                ) from exc
        return replayed

    @staticmethod
    def _apply_record(
        replayed: _ReplayedState, kind: str, record: dict[str, Any]
    ) -> None:
        if kind == "claim":
            FileLedger._apply_claim(replayed, record)
        elif kind == "intent":
            FileLedger._apply_intent(replayed, record)
        elif kind == "outcome":
            FileLedger._apply_outcome(replayed, record)
        elif kind == "reconcile":
            FileLedger._apply_reconcile(replayed, record)
        elif kind == "negative_matrix":
            replayed.negative_matrix_receipts.append(
                NegativeMatrixReceipt(
                    evidence_ref=record["evidence_ref"],
                    repository=record["repository"],
                    contract_digest=record["contract_digest"],
                    contract_edition=record["contract_edition"],
                    pilot_source_ref=record["pilot_source_ref"],
                    production_source_ref=record["production_source_ref"],
                    verified_at=_parse_timestamp(record["verified_at"]),
                    approver_identity=record["approver_identity"],
                    evidence_hash=record["evidence_hash"],
                )
            )
        else:
            raise LedgerJournalError(f"unknown journal record kind: {kind!r}")

    @staticmethod
    def _apply_claim(replayed: _ReplayedState, record: dict[str, Any]) -> None:
        fp = record["operation_fingerprint"]
        if fp in replayed.claims:
            raise LedgerJournalError(f"duplicate claim record for {fp}")
        target = TargetName(record["target_key"])
        kind = ReleaseAction(record["operation_kind"])
        replayed.claims[fp] = ClaimReceipt(
            disposition=ClaimDisposition.OWNER,
            external_id=record["external_id"],
            authorization_id=record["authorization_id"],
            operation_fingerprint=fp,
            target_key=target,
            artifact_digest=record["artifact_digest"],
            operation_kind=kind,
            contract_digest=record["contract_digest"],
            evidence_hash=record["evidence_hash"],
            state=OperationState.CLAIMED_LOCKED,
            state_version=1,
            fencing_token=record["fencing_token"],
            durable=True,
            authority_use_burned=True,
            claim_id=record["claim_id"],
        )
        replayed.states[fp] = LedgerStateSnapshot(
            external_id=record["external_id"],
            claim_id=record["claim_id"],
            operation_fingerprint=fp,
            target=target,
            artifact_digest=record["artifact_digest"],
            contract_digest=record["contract_digest"],
            state=OperationState.CLAIMED_LOCKED,
            state_version=1,
            fencing_token=record["fencing_token"],
            uncertainty_started_at=_parse_timestamp(record["recorded_at"]),
        )

    @staticmethod
    def _apply_intent(replayed: _ReplayedState, record: dict[str, Any]) -> None:
        fp = record["operation_fingerprint"]
        prior = replayed.states.get(fp)
        if prior is None:
            raise LedgerJournalError(f"intent record for unclaimed operation {fp}")
        replayed.states[fp] = dataclasses.replace(
            prior,
            state_version=record["state_version"],
            provider_intent_hash=record["provider_intent_hash"],
            provider_not_before=_parse_timestamp(record["not_before"]),
            provider_not_after=_parse_timestamp(record["not_after"]),
        )

    @staticmethod
    def _apply_outcome(replayed: _ReplayedState, record: dict[str, Any]) -> None:
        fp = record["operation_fingerprint"]
        prior = replayed.states.get(fp)
        if prior is None:
            raise LedgerJournalError(f"outcome record for unclaimed operation {fp}")
        next_state = OperationState(record["next_state"])
        replayed.states[fp] = dataclasses.replace(
            prior, state=next_state, state_version=record["state_version"]
        )
        replayed.outcome_fields[fp] = record
        # Matches FakeLedger.record_outcome: only a direct outcome of
        # APPLIED materializes a receipt here. OBSERVED_APPLIED only ever
        # follows a later `observe` confirmation, recorded via reconcile.
        if next_state is OperationState.APPLIED:
            FileLedger._maybe_store_pilot_receipt(
                replayed, fp, next_state, record, record["recorded_at"]
            )

    @staticmethod
    def _apply_reconcile(replayed: _ReplayedState, record: dict[str, Any]) -> None:
        fp = record["operation_fingerprint"]
        prior = replayed.states.get(fp)
        if prior is None:
            raise LedgerJournalError(f"reconcile record for unclaimed operation {fp}")
        next_state = OperationState(record["next_state"])
        replayed.states[fp] = dataclasses.replace(
            prior, state=next_state, state_version=record["state_version"]
        )
        outcome = replayed.outcome_fields.get(fp)
        if outcome is not None and next_state is OperationState.OBSERVED_APPLIED:
            FileLedger._maybe_store_pilot_receipt(
                replayed, fp, next_state, outcome, record["recorded_at"]
            )

    @staticmethod
    def _maybe_store_pilot_receipt(
        replayed: _ReplayedState,
        fp: str,
        next_state: OperationState,
        outcome: dict[str, Any],
        confirmed_at: str,
    ) -> None:
        if next_state not in (OperationState.APPLIED, OperationState.OBSERVED_APPLIED):
            return
        if TargetName(outcome["target"]) is not TargetName.PILOT:
            return
        claim = replayed.claims.get(fp)
        state = replayed.states[fp]
        if claim is None:
            return
        receipt = AppliedReleaseReceipt(
            receipt_id=str(uuid.uuid4()),
            claim_id=claim.claim_id,
            repository=outcome["repository"],
            release_id=outcome["release_id"],
            manifest_release_id=outcome["manifest_release_id"],
            operation_fingerprint=fp,
            action=ReleaseAction(outcome["action"]),
            target=TargetName.PILOT,
            artifact_digest=outcome["artifact_digest"],
            embedded_manifest_hash=outcome["embedded_manifest_hash"],
            contract_digest=outcome["contract_digest"],
            contract_edition=outcome["contract_edition"],
            required_checks_digest=outcome["required_checks_digest"],
            state=next_state,
            state_version=state.state_version,
            fencing_token=state.fencing_token,
            durable=True,
            effect_confirmed_at=_parse_timestamp(confirmed_at),
            provider_evidence_hash=outcome["evidence_hash"],
        )
        key = tuple(getattr(receipt, field) for field in _PILOT_RECEIPT_FIELDS)
        replayed.pilot_receipts[key] = receipt

    def _append(self, record: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            target.flush()
            os.fsync(target.fileno())

    # -- ReleaseLedgerPort --------------------------------------------------

    def negative_matrix_receipt(
        self, query: NegativeMatrixQuery
    ) -> NegativeMatrixReceipt | None:
        with _journal_lock(self._lock_path):
            replayed = self._replay()
        wanted = tuple(getattr(query, field) for field in _NEGATIVE_MATRIX_FIELDS)
        for receipt in replayed.negative_matrix_receipts:
            if (
                tuple(getattr(receipt, field) for field in _NEGATIVE_MATRIX_FIELDS)
                == wanted
            ):
                return receipt
        return None

    def record_negative_matrix_receipt(self, receipt: NegativeMatrixReceipt) -> None:
        """Not part of ReleaseLedgerPort: the one-time attestation write path."""

        with _journal_lock(self._lock_path):
            self._append(
                {
                    "kind": "negative_matrix",
                    "evidence_ref": receipt.evidence_ref,
                    "repository": receipt.repository,
                    "contract_digest": receipt.contract_digest,
                    "contract_edition": receipt.contract_edition,
                    "pilot_source_ref": receipt.pilot_source_ref,
                    "production_source_ref": receipt.production_source_ref,
                    "verified_at": _timestamp(receipt.verified_at),
                    "approver_identity": receipt.approver_identity,
                    "evidence_hash": receipt.evidence_hash,
                }
            )

    def pilot_applied_receipt(
        self, query: PilotReceiptQuery
    ) -> AppliedReleaseReceipt | None:
        with _journal_lock(self._lock_path):
            replayed = self._replay()
        key = tuple(getattr(query, field) for field in _PILOT_RECEIPT_FIELDS)
        return replayed.pilot_receipts.get(key)

    def claim_and_burn(self, request: LedgerClaimRequest) -> ClaimReceipt:
        with _journal_lock(self._lock_path):
            replayed = self._replay()
            fp = request.operation_fingerprint
            existing = replayed.claims.get(fp)
            if existing is not None:
                state = replayed.states[fp]
                return dataclasses.replace(
                    existing,
                    disposition=ClaimDisposition.DUPLICATE,
                    state=state.state,
                    state_version=state.state_version,
                )
            fencing_token = replayed.max_fencing_token + 1
            claim_id = str(uuid.uuid4())
            recorded_at = _now()
            self._append(
                {
                    "kind": "claim",
                    "external_id": request.external_id,
                    "authorization_id": request.authorization_id,
                    "operation_fingerprint": fp,
                    "target_key": request.target_key.value,
                    "artifact_digest": request.artifact_digest,
                    "operation_kind": request.operation_kind.value,
                    "contract_digest": request.contract_digest,
                    "evidence_hash": request.evidence_hash,
                    "claim_id": claim_id,
                    "fencing_token": fencing_token,
                    "recorded_at": recorded_at,
                }
            )
            return ClaimReceipt(
                disposition=ClaimDisposition.OWNER,
                external_id=request.external_id,
                authorization_id=request.authorization_id,
                operation_fingerprint=fp,
                target_key=request.target_key,
                artifact_digest=request.artifact_digest,
                operation_kind=request.operation_kind,
                contract_digest=request.contract_digest,
                evidence_hash=request.evidence_hash,
                state=OperationState.CLAIMED_LOCKED,
                state_version=1,
                fencing_token=fencing_token,
                durable=True,
                authority_use_burned=True,
                claim_id=claim_id,
            )

    def record_provider_intent(
        self, request: ProviderIntentRecordRequest
    ) -> LedgerStateSnapshot | None:
        with _journal_lock(self._lock_path):
            replayed = self._replay()
            fp = request.operation_fingerprint
            state = replayed.states.get(fp)
            if (
                state is None
                or state.external_id != request.external_id
                or state.claim_id != request.claim_id
                or not self._matches_cas(state, request)
            ):
                return None
            new_version = state.state_version + 1
            self._append(
                {
                    "kind": "intent",
                    "operation_fingerprint": fp,
                    "provider_intent_hash": request.provider_intent_hash,
                    "not_before": _timestamp(request.not_before),
                    "not_after": _timestamp(request.not_after),
                    "state_version": new_version,
                    "fencing_token": request.fencing_token,
                }
            )
            return dataclasses.replace(
                state,
                state_version=new_version,
                provider_intent_hash=request.provider_intent_hash,
                provider_not_before=request.not_before,
                provider_not_after=request.not_after,
            )

    def record_outcome(
        self, request: LedgerOutcomeRequest
    ) -> LedgerStateSnapshot | None:
        with _journal_lock(self._lock_path):
            replayed = self._replay()
            fp = request.operation_fingerprint
            state = replayed.states.get(fp)
            if state is None or not self._matches_cas(state, request):
                return None
            if state.claim_id != request.claim_id:
                return None
            new_version = state.state_version + 1
            recorded_at = _now()
            self._append(
                {
                    "kind": "outcome",
                    "external_id": request.external_id,
                    "claim_id": request.claim_id,
                    "operation_fingerprint": fp,
                    "next_state": request.next_state.value,
                    "state_version": new_version,
                    "fencing_token": request.fencing_token,
                    "target": request.target.value,
                    "repository": request.repository,
                    "release_id": request.release_id,
                    "manifest_release_id": request.manifest_release_id,
                    "action": request.action.value,
                    "artifact_digest": request.artifact_digest,
                    "embedded_manifest_hash": request.embedded_manifest_hash,
                    "contract_digest": request.contract_digest,
                    "contract_edition": request.contract_edition,
                    "required_checks_digest": request.required_checks_digest,
                    "evidence_hash": request.evidence_hash,
                    "recorded_at": recorded_at,
                }
            )
            return dataclasses.replace(
                state, state=request.next_state, state_version=new_version
            )

    def state_snapshot(self, operation_fingerprint: str) -> LedgerStateSnapshot | None:
        with _journal_lock(self._lock_path):
            replayed = self._replay()
        return replayed.states.get(operation_fingerprint)

    def reconcile_compare_and_swap(
        self, request: ReconciliationCASRequest
    ) -> LedgerStateSnapshot | None:
        with _journal_lock(self._lock_path):
            replayed = self._replay()
            fp = request.operation_fingerprint
            state = replayed.states.get(fp)
            if state is None or not self._matches_cas(state, request):
                return None
            new_version = state.state_version + 1
            self._append(
                {
                    "kind": "reconcile",
                    "external_id": request.external_id,
                    "operation_fingerprint": fp,
                    "next_state": request.next_state.value,
                    "state_version": new_version,
                    "fencing_token": request.fencing_token,
                    "evidence_hash": request.evidence_hash,
                    "observation_reference_hashes": list(
                        request.observation_reference_hashes
                    ),
                    "recorded_at": _now(),
                }
            )
            return dataclasses.replace(
                state, state=request.next_state, state_version=new_version
            )

    @staticmethod
    def _matches_cas(state: LedgerStateSnapshot, request: Any) -> bool:
        return (
            state.state is request.expected_state
            and state.state_version == request.expected_version
            and state.fencing_token == request.fencing_token
        )
