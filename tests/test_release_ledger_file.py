"""Tests for the durable file-backed ReleaseLedgerPort (WP-562 Ф3.1).

Peer-session 2026-10-14-02-wp562-f3-1-ledger (Claude+Codex) agreed the
journal must be the only source of truth for every CAS decision and that an
index on the decision path cannot be trusted -- these tests check exactly
that property, not just the happy path a mock would already give for free.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime, timedelta

import pytest
from scripts.release_control import (
    ClaimDisposition,
    LedgerClaimRequest,
    LedgerOutcomeRequest,
    NegativeMatrixQuery,
    NegativeMatrixReceipt,
    OperationState,
    PilotReceiptQuery,
    ProviderIntentRecordRequest,
    ReconciliationCASRequest,
    ReleaseAction,
    TargetName,
    operation_external_id,
)
from scripts.release_ledger_file import FileLedger, LedgerJournalError

NOW = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def _fingerprint(tag: str) -> str:
    return "sha256:" + tag.ljust(64, "0")


def _claim_request(fingerprint: str = _fingerprint("1")) -> LedgerClaimRequest:
    return LedgerClaimRequest(
        external_id=operation_external_id(fingerprint),
        authorization_id="auth-1",
        operation_fingerprint=fingerprint,
        target_key=TargetName.PILOT,
        artifact_digest=DIGEST_A,
        operation_kind=ReleaseAction.PROMOTE,
        contract_digest=DIGEST_B,
        evidence_hash=DIGEST_A,
    )


def test_claim_and_burn_is_owner_then_duplicate(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    request = _claim_request()

    first = ledger.claim_and_burn(request)
    assert first.disposition is ClaimDisposition.OWNER
    assert first.state is OperationState.CLAIMED_LOCKED
    assert first.fencing_token >= 1

    second = ledger.claim_and_burn(request)
    assert second.disposition is ClaimDisposition.DUPLICATE
    assert second.claim_id == first.claim_id
    assert second.fencing_token == first.fencing_token


def test_claim_is_durable_across_fresh_ledger_instances(tmp_path):
    path = tmp_path / "ledger.jsonl"
    request = _claim_request()
    first_instance = FileLedger(path)
    claimed = first_instance.claim_and_burn(request)

    fresh_instance = FileLedger(path)
    duplicate = fresh_instance.claim_and_burn(request)

    assert duplicate.disposition is ClaimDisposition.DUPLICATE
    assert duplicate.claim_id == claimed.claim_id


def test_fencing_token_is_monotonic_across_operations(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    first = ledger.claim_and_burn(_claim_request(_fingerprint("1")))
    second = ledger.claim_and_burn(_claim_request(_fingerprint("2")))
    assert second.fencing_token > first.fencing_token


def test_record_outcome_rejects_stale_fencing_token(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    claim = ledger.claim_and_burn(_claim_request())

    stale_request = LedgerOutcomeRequest(
        external_id=claim.external_id,
        claim_id=claim.claim_id,
        operation_fingerprint=claim.operation_fingerprint,
        expected_state=OperationState.CLAIMED_LOCKED,
        expected_version=claim.state_version,
        fencing_token=claim.fencing_token + 1,  # wrong on purpose
        next_state=OperationState.APPLIED,
        target=TargetName.PILOT,
        repository="aisystant/aist_bot",
        release_id="release-1",
        manifest_release_id="release-1",
        action=ReleaseAction.PROMOTE,
        artifact_digest=DIGEST_A,
        embedded_manifest_hash=DIGEST_B,
        contract_digest=DIGEST_B,
        contract_edition="edition-1",
        required_checks_digest=DIGEST_A,
        behavior_scenarios_digest=DIGEST_C,
        evidence_hash=DIGEST_A,
    )
    assert ledger.record_outcome(stale_request) is None


def test_record_outcome_to_applied_exposes_pilot_receipt(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    claim = ledger.claim_and_burn(_claim_request())

    outcome_request = LedgerOutcomeRequest(
        external_id=claim.external_id,
        claim_id=claim.claim_id,
        operation_fingerprint=claim.operation_fingerprint,
        expected_state=OperationState.CLAIMED_LOCKED,
        expected_version=1,
        fencing_token=claim.fencing_token,
        next_state=OperationState.APPLIED,
        target=TargetName.PILOT,
        repository="aisystant/aist_bot",
        release_id="release-1",
        manifest_release_id="release-1",
        action=ReleaseAction.PROMOTE,
        artifact_digest=DIGEST_A,
        embedded_manifest_hash=DIGEST_B,
        contract_digest=DIGEST_B,
        contract_edition="edition-1",
        required_checks_digest=DIGEST_A,
        behavior_scenarios_digest=DIGEST_C,
        evidence_hash=DIGEST_A,
    )
    updated = ledger.record_outcome(outcome_request)
    assert updated is not None
    assert updated.state is OperationState.APPLIED

    receipt = ledger.pilot_applied_receipt(
        PilotReceiptQuery(
            repository="aisystant/aist_bot",
            manifest_release_id="release-1",
            operation_fingerprint=claim.operation_fingerprint,
            artifact_digest=DIGEST_A,
            embedded_manifest_hash=DIGEST_B,
            contract_digest=DIGEST_B,
            contract_edition="edition-1",
            required_checks_digest=DIGEST_A,
        )
    )
    assert receipt is not None
    assert receipt.state is OperationState.APPLIED
    assert receipt.durable is True
    assert receipt.behavior_scenarios_digest == DIGEST_C


def test_reconcile_compare_and_swap_rejects_stale_version(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    claim = ledger.claim_and_burn(_claim_request())

    stale_reconcile = ReconciliationCASRequest(
        external_id=claim.external_id,
        operation_fingerprint=claim.operation_fingerprint,
        expected_state=OperationState.CLAIMED_LOCKED,
        expected_version=99,  # wrong on purpose
        fencing_token=claim.fencing_token,
        next_state=OperationState.OBSERVED_NOT_APPLIED,
        evidence_hash=DIGEST_A,
        observation_reference_hashes=(DIGEST_A,),
    )
    assert ledger.reconcile_compare_and_swap(stale_reconcile) is None


def test_negative_matrix_receipt_round_trips(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    query = NegativeMatrixQuery(
        evidence_ref=DIGEST_A,
        repository="aisystant/aist_bot",
        contract_digest=DIGEST_B,
        contract_edition="edition-1",
        pilot_source_ref="refs/heads/pilot",
        production_source_ref="refs/heads/new-architecture",
    )
    assert ledger.negative_matrix_receipt(query) is None

    ledger.record_negative_matrix_receipt(
        NegativeMatrixReceipt(
            evidence_ref=query.evidence_ref,
            repository=query.repository,
            contract_digest=query.contract_digest,
            contract_edition=query.contract_edition,
            pilot_source_ref=query.pilot_source_ref,
            production_source_ref=query.production_source_ref,
            verified_at=NOW,
            approver_identity="pilot",
            evidence_hash=DIGEST_A,
        )
    )
    receipt = ledger.negative_matrix_receipt(query)
    assert receipt is not None
    assert receipt.approver_identity == "pilot"


def test_malformed_journal_record_stops_cas_instead_of_skipping(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"kind": "claim", "operation_fingerprint": "fp-1"}\n')
    ledger = FileLedger(path)
    with pytest.raises(LedgerJournalError):
        ledger.state_snapshot("fp-1")


def test_outcome_record_predating_behavior_scenarios_digest_fails_closed(tmp_path):
    """An outcome record written before WP-562 Ф7 added the field is a
    malformed record, same as any other missing required field -- it must
    raise LedgerJournalError (fail-closed), never a raw KeyError."""

    path = tmp_path / "ledger.jsonl"
    ledger = FileLedger(path)
    claim = ledger.claim_and_burn(_claim_request())
    outcome_request = LedgerOutcomeRequest(
        external_id=claim.external_id,
        claim_id=claim.claim_id,
        operation_fingerprint=claim.operation_fingerprint,
        expected_state=OperationState.CLAIMED_LOCKED,
        expected_version=1,
        fencing_token=claim.fencing_token,
        next_state=OperationState.APPLIED,
        target=TargetName.PILOT,
        repository="aisystant/aist_bot",
        release_id="release-1",
        manifest_release_id="release-1",
        action=ReleaseAction.PROMOTE,
        artifact_digest=DIGEST_A,
        embedded_manifest_hash=DIGEST_B,
        contract_digest=DIGEST_B,
        contract_edition="edition-1",
        required_checks_digest=DIGEST_A,
        behavior_scenarios_digest=DIGEST_C,
        evidence_hash=DIGEST_A,
    )
    assert ledger.record_outcome(outcome_request) is not None

    # Simulate a pre-WP-562-Ф7 journal: strip the field the old writer never
    # emitted, the way it would look if replayed unmodified from history.
    lines = path.read_text().splitlines()
    outcome_line_index = next(
        index
        for index, line in enumerate(lines)
        if '"kind": "outcome"' in line
    )
    outcome_record = json.loads(lines[outcome_line_index])
    del outcome_record["behavior_scenarios_digest"]
    lines[outcome_line_index] = json.dumps(outcome_record)
    path.write_text("\n".join(lines) + "\n")

    stale_ledger = FileLedger(path)
    with pytest.raises(LedgerJournalError, match="behavior_scenarios_digest"):
        stale_ledger.state_snapshot(claim.operation_fingerprint)


def test_provider_intent_rejects_mismatched_claim_id(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.jsonl")
    claim = ledger.claim_and_burn(_claim_request())

    wrong_request = ProviderIntentRecordRequest(
        external_id=claim.external_id,
        claim_id="00000000-0000-4000-8000-000000000099",  # valid UUID, wrong claim
        operation_fingerprint=claim.operation_fingerprint,
        expected_state=OperationState.CLAIMED_LOCKED,
        expected_version=1,
        fencing_token=claim.fencing_token,
        provider_intent_hash=DIGEST_A,
        not_before=NOW,
        not_after=NOW + timedelta(minutes=5),
    )
    assert ledger.record_provider_intent(wrong_request) is None

    right_request = dataclasses_replace(wrong_request, claim_id=claim.claim_id)
    result = ledger.record_provider_intent(right_request)
    assert result is not None
    assert result.provider_intent_hash == DIGEST_A


def test_concurrent_claim_from_two_real_processes_yields_one_owner(tmp_path):
    """A thread-based test cannot exercise fcntl.flock across processes.

    Cold review (2026-10-14) found the first version of this test used
    `subprocess.run()` inside a list comprehension -- which blocks until each
    child exits, so the two processes never actually overlapped. Fixed with
    `Popen` plus a barrier directory: each child announces readiness by
    creating a marker file, then spins until BOTH markers exist before
    calling `claim_and_burn`, forcing real contention on the same `flock`.
    """

    path = tmp_path / "ledger.jsonl"
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    script = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(__file__.rsplit('/tests/', 1)[0])!r})
from scripts.release_control import LedgerClaimRequest, ReleaseAction, TargetName
from scripts.release_ledger_file import FileLedger

barrier_dir = Path({str(barrier_dir)!r})
(barrier_dir / f"ready-{{sys.argv[1]}}").touch()
deadline = time.monotonic() + 10
while len(list(barrier_dir.iterdir())) < 2:
    if time.monotonic() > deadline:
        raise SystemExit("barrier timed out -- other process never arrived")
    time.sleep(0.01)

ledger = FileLedger(Path({str(path)!r}))
request = LedgerClaimRequest(
    external_id="rc1-race",
    authorization_id="auth-1",
    operation_fingerprint="fp-race",
    target_key=TargetName.PILOT,
    artifact_digest={DIGEST_A!r},
    operation_kind=ReleaseAction.PROMOTE,
    contract_digest={DIGEST_B!r},
    evidence_hash={DIGEST_A!r},
)
receipt = ledger.claim_and_burn(request)
print(receipt.disposition.value)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    results = [process.communicate(timeout=30) for process in processes]
    for process, (_, stderr) in zip(processes, results):
        assert process.returncode == 0, stderr
    dispositions = [stdout.strip() for stdout, _ in results]
    assert sorted(dispositions) == ["duplicate", "owner"]
