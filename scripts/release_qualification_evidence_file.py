"""Durable file-backed QualificationEvidencePort (WP-562 Ф3.4).

Peer-session 2026-10-14-17-wp562-release-contour (Claude+Kimi+Codex).

Scope finding from this session (see 02-writer.md / 02-peer-kimi.md in the
session folder): by the time `record_durable()` is called,
`finalize_production_operation()` in release_control.py has already built
the request entirely from a `PilotQualificationReceipt` that passed
`_validate_pilot_qualification()` -- trust/signature verification is the
job of the `VerifiedPilotQualificationResolverPort` upstream (port 7, not
implemented in this session), not this port. This port's only job is to be
an honest durable sink: store exactly what it was given, and echo it back
unchanged. `_validate_durable_qualification()` in release_control.py
enforces that echo-back field-by-field plus a `recorded_at >=
qualification_verified_at` ordering invariant -- this implementation exists
to satisfy that check, not to re-verify evidence it was never handed in the
first place.

Same journal discipline as `release_ledger_file.py` (Ф3.1): one append-only
JSON-lines file under a `fcntl.flock`, full replay from the start is the
only path any read trusts (no index, no cache). Idempotency key is
`request_digest` -- the same qualification request recorded twice (e.g. a
retried call after a transient failure) returns the SAME stored receipt
rather than appending a duplicate; `state_version` is the 1-based position
of that record in the journal at append time, giving a monotonic ordering
witness without needing a CAS (this port has no claim/outcome lifecycle to
protect, unlike the release ledger).
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import uuid
from datetime import UTC, datetime
from typing import Any

from release_manifest import canonical_sha256
from scripts.release_control import (
    DurableQualificationReceipt,
    QualificationRecordRequest,
)

DEFAULT_QUALIFICATION_EVIDENCE_PATH = (
    pathlib.Path.home() / "IWE/DS-my-strategy/machine/qualification-evidence.jsonl"
)


class QualificationEvidenceJournalError(RuntimeError):
    """The journal cannot be trusted; refuse the record instead of guessing."""


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise QualificationEvidenceJournalError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise QualificationEvidenceJournalError("timestamp must include a timezone")
    return parsed


class FileQualificationEvidence:
    """Append-only, file-locked QualificationEvidencePort (single host)."""

    def __init__(
        self, path: pathlib.Path = DEFAULT_QUALIFICATION_EVIDENCE_PATH
    ) -> None:
        self._path = path
        self._lock_path = path.with_suffix(path.suffix + ".lock")

    def record_durable(
        self, request: QualificationRecordRequest
    ) -> DurableQualificationReceipt:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                records = self._read_records()
                existing = self._find_by_request_digest(
                    records, request.request_digest
                )
                if existing is not None:
                    return self._receipt_from_record(existing)
                state_version = len(records) + 1
                recorded_at = datetime.now(UTC)
                record = self._build_record(request, state_version, recorded_at)
                with self._path.open("a", encoding="utf-8") as sink:
                    sink.write(json.dumps(record, sort_keys=True))
                    sink.write("\n")
                    sink.flush()
                    os.fsync(sink.fileno())
                return self._receipt_from_record(record)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

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
                    raise QualificationEvidenceJournalError(
                        f"malformed journal record at line {line_number}: {exc}"
                    ) from exc
                if not isinstance(record, dict) or "request_digest" not in record:
                    raise QualificationEvidenceJournalError(
                        f"journal record at line {line_number} is missing "
                        "'request_digest'"
                    )
                records.append(record)
        return records

    @staticmethod
    def _find_by_request_digest(
        records: list[dict[str, Any]], request_digest: str
    ) -> dict[str, Any] | None:
        for record in records:
            if record["request_digest"] == request_digest:
                return record
        return None

    @staticmethod
    def _build_record(
        request: QualificationRecordRequest,
        state_version: int,
        recorded_at: datetime,
    ) -> dict[str, Any]:
        record_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"qualification-evidence:{request.request_digest}"
            )
        )
        return {
            "record_id": record_id,
            "request_digest": request.request_digest,
            "repository": request.repository,
            "contract_digest": request.contract_digest,
            "contract_edition": request.contract_edition,
            "release_id": request.release_id,
            "pilot_claim_id": request.pilot_claim_id,
            "pilot_operation_fingerprint": request.pilot_operation_fingerprint,
            "pilot_applied_receipt_hash": request.pilot_applied_receipt_hash,
            "qualification_receipt_hash": request.qualification_receipt_hash,
            "external_receipt_hash": request.external_receipt_hash,
            "artifact_digest": request.artifact_digest,
            "embedded_manifest_hash": request.embedded_manifest_hash,
            "verifier_identity": request.verifier_identity,
            "qualification_verified_at": _timestamp(
                request.qualification_verified_at
            ),
            "recorded_at": _timestamp(recorded_at),
            "state_version": state_version,
        }

    @staticmethod
    def _receipt_from_record(record: dict[str, Any]) -> DurableQualificationReceipt:
        evidence_hash = canonical_sha256(
            {
                "request_digest": record["request_digest"],
                "record_id": record["record_id"],
                "recorded_at": record["recorded_at"],
            }
        )
        return DurableQualificationReceipt(
            schema_version=1,
            record_id=record["record_id"],
            request_digest=record["request_digest"],
            repository=record["repository"],
            contract_digest=record["contract_digest"],
            contract_edition=record["contract_edition"],
            release_id=record["release_id"],
            pilot_claim_id=record["pilot_claim_id"],
            pilot_operation_fingerprint=record["pilot_operation_fingerprint"],
            pilot_applied_receipt_hash=record["pilot_applied_receipt_hash"],
            qualification_receipt_hash=record["qualification_receipt_hash"],
            external_receipt_hash=record["external_receipt_hash"],
            artifact_digest=record["artifact_digest"],
            embedded_manifest_hash=record["embedded_manifest_hash"],
            verifier_identity=record["verifier_identity"],
            qualification_verified_at=_parse_timestamp(
                record["qualification_verified_at"]
            ),
            recorded_at=_parse_timestamp(record["recorded_at"]),
            state_version=record["state_version"],
            durable=True,
            evidence_hash=evidence_hash,
        )
