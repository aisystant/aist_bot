from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from release_manifest import ReleaseManifestError
from scripts.release_control import (
    AUTHORITY_AUDIENCE,
    AUTHORITY_PURPOSE,
    OCI_INDEX_MEDIA_TYPE,
    OCI_MANIFEST_MEDIA_TYPE,
    AppliedReleaseReceipt,
    ArtifactDescriptor,
    ArtifactPurpose,
    ArtifactResolutionReceipt,
    ArtifactResolutionRequest,
    AuthorityEvidence,
    AuthorityRejected,
    AuthorityVerificationReceipt,
    CheckResult,
    CheckSuiteReceipt,
    CheckSuiteVerificationRequest,
    ClaimDisposition,
    ClaimReceipt,
    ContractError,
    CutoverBlocked,
    DurableQualificationReceipt,
    EmbeddedManifest,
    EvidenceRejected,
    ExecutionState,
    HistoricalBuildContractReceipt,
    LedgerClaimRequest,
    LedgerOutcomeRequest,
    LedgerSafetyError,
    LedgerStateSnapshot,
    MigrationClass,
    NegativeMatrixReceipt,
    ObservationEnvelope,
    OperationState,
    PilotQualificationReceipt,
    PilotSignalResult,
    ProviderApplyResult,
    ProviderMutationRequest,
    ProviderObservation,
    QualificationRecordRequest,
    ReconciliationCASRequest,
    ReleaseAction,
    ReleaseControlContract,
    ReleaseOperation,
    RollbackCapabilityReceipt,
    RollbackCompatibilityReceipt,
    RollbackRejected,
    SchemaError,
    SourceCandidateReceipt,
    SourceCandidateVerificationRequest,
    TargetName,
    TargetSnapshotReceipt,
    TargetSnapshotRequest,
    canonical_json_bytes,
    canonical_sha256,
    execute_operation,
    finalize_production_operation,
    load_release_control_contract,
    loads_strict_json,
    operation_external_id,
    plan_build_once_promotion,
    plan_rollback,
    provider_idempotency_key,
    reconcile_uncertain_operation,
    validate_immutable_image_reference,
    validate_release_control_contract,
)

REPO_ROOT = Path(__file__).parents[1]
CONTRACT_PATH = REPO_ROOT / ".github" / "release-control-contract.json"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
RUNNER = "release-runner"
APPROVER = "pilot-approver"
AUTHORITY_ID = "10000000-0000-4000-8000-000000000001"
CLAIM_ID = "20000000-0000-4000-8000-000000000001"
PILOT_RECEIPT_ID = "30000000-0000-4000-8000-000000000001"


def _sha(character: str) -> str:
    return "sha256:" + character * 64


DIGEST_A = _sha("a")
DIGEST_B = _sha("b")
DIGEST_C = _sha("c")
DIGEST_D = _sha("d")
DIGEST_E = _sha("e")
DIGEST_F = _sha("f")
NEGATIVE_MATRIX_EVIDENCE_REF = _sha("9")
SOURCE_COMMIT = "1" * 40
SOURCE_TREE = "2" * 40


def _contract(*, cutover: bool):
    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    payload["cutover"]["enabled"] = cutover
    if cutover:
        payload["cutover"]["negative_matrix_evidence_ref"] = (
            NEGATIVE_MATRIX_EVIDENCE_REF
        )
    return validate_release_control_contract(payload)


def _manifest(contract, **overrides: object) -> EmbeddedManifest:
    payload: dict[str, object] = {
        "schema_version": 1,
        "release_id": "release-2026-09-03-01",
        "source_commit": SOURCE_COMMIT,
        "source_tree": SOURCE_TREE,
        "build_contract_digest": contract.contract_digest,
        "migration_class": "expand",
        "schema_min": 4,
        "schema_max": 6,
        "required_checks": list(contract.required_checks),
        "platform": {"os": "linux", "architecture": "amd64"},
    }
    payload.update(overrides)
    return EmbeddedManifest.from_mapping(payload)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.calls = 0

    def now_utc(self) -> datetime:
        self.calls += 1
        return self.now


class FailingClock:
    def now_utc(self) -> datetime:
        raise RuntimeError("trusted time unavailable")


class SequenceClock:
    def __init__(self, *results: datetime | Exception) -> None:
        self.results = results
        self.calls = 0

    def now_utc(self) -> datetime:
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


class FakeArtifactResolver:
    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls = 0
        self.requests = []

    def resolve_verified(self, request):
        self.calls += 1
        self.requests.append(request)
        values = {
            "schema_version": 1,
            "receipt_id": "40000000-0000-4000-8000-000000000001",
            "request_digest": request.request_digest,
            "repository": request.repository,
            "contract_digest": request.contract_digest,
            "contract_edition": request.contract_edition,
            "release_id": request.release_id,
            "artifact_digest": DIGEST_A,
            "digest_kind": "oci_manifest",
            "media_type": OCI_MANIFEST_MEDIA_TYPE,
            "platform_os": "linux",
            "platform_architecture": "amd64",
            "image_reference": f"{request.oci_repository}@{DIGEST_A}",
            "embedded_layer_digest": DIGEST_B,
            "embedded_content_hash": request.embedded_manifest_hash,
            "provenance": (
                "build_once"
                if request.purpose is ArtifactPurpose.PROMOTION
                else "retained"
            ),
            "resolver_identity": "artifact-verifier",
            "resolved_at": NOW - timedelta(seconds=5),
            "evidence_hash": DIGEST_C,
        }
        values.update(self.overrides)
        if values.pop("return_untyped", False):
            return values
        return ArtifactResolutionReceipt(**values)


class FakeTargetResolver:
    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls = 0
        self.requests = []

    def snapshot_verified(self, request):
        self.calls += 1
        self.requests.append(request)
        is_pilot = request.target is TargetName.PILOT
        values = {
            "schema_version": 1,
            "snapshot_id": (
                "50000000-0000-4000-8000-000000000001"
                if is_pilot
                else "50000000-0000-4000-8000-000000000002"
            ),
            "request_digest": request.request_digest,
            "repository": request.repository,
            "contract_digest": request.contract_digest,
            "contract_edition": request.contract_edition,
            "operation_release_id": request.operation_release_id,
            "candidate_release_id": request.candidate_release_id,
            "target": request.target,
            "candidate_artifact_digest": request.candidate_artifact_digest,
            "candidate_manifest_hash": request.candidate_manifest_hash,
            "target_profile_digest": DIGEST_D if is_pilot else DIGEST_E,
            "current_artifact_digest": DIGEST_B if is_pilot else DIGEST_C,
            "current_manifest_hash": DIGEST_F,
            "target_config_digest": DIGEST_D,
            "current_schema": 5,
            "migration_history_digest": DIGEST_E,
            "migration_class": request.migration_class,
            "schema_compatible": True,
            "migration_allowed": True,
            "complete": True,
            "captured_at": NOW - timedelta(seconds=5),
            "source_identity": "target-state-verifier",
            "evidence_hash": DIGEST_F,
        }
        values.update(self.overrides)
        if values.pop("return_untyped", False):
            return values
        return TargetSnapshotReceipt(**values)


class ForgedCheckSuiteReceipt(CheckSuiteReceipt):
    pass


def _check_results(
    check_ids,
    *,
    conclusion: str = "success",
) -> tuple[CheckResult, ...]:
    return tuple(
        CheckResult(
            check_id=check_id,
            conclusion=conclusion,
            completed_at=NOW - timedelta(seconds=10),
            suite_reference_hash=DIGEST_C,
            provider_reference_hash=_sha(format(index % 16, "x")),
            evidence_hash=_sha(format((index + 8) % 16, "x")),
        )
        for index, check_id in enumerate(check_ids)
    )


class FakeCheckSuiteResolver:
    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls = 0
        self.requests = []

    def resolve_verified(self, request):
        self.calls += 1
        self.requests.append(request)
        results = _check_results(request.required_checks)
        values = {
            "schema_version": 1,
            "receipt_id": "45000000-0000-4000-8000-000000000001",
            "request_digest": request.request_digest,
            "repository": request.repository,
            "contract_digest": request.contract_digest,
            "contract_edition": request.contract_edition,
            "release_id": request.release_id,
            "source_commit": request.source_commit,
            "source_tree": request.source_tree,
            "embedded_manifest_hash": request.embedded_manifest_hash,
            "artifact_digest": request.artifact_digest,
            "required_checks": request.required_checks,
            "forbidden_paths": request.forbidden_paths,
            "complete": True,
            "forbidden_paths_absent": True,
            "suite_reference_hash": DIGEST_C,
            "results": results,
            "verified_at": NOW - timedelta(seconds=5),
            "verifier_identity": "check-suite-verifier",
            "evidence_hash": DIGEST_F,
        }
        values.update(self.overrides)
        return_untyped = values.pop("return_untyped", False)
        return_subclass = values.pop("return_subclass", False)
        if return_untyped:
            return values
        receipt_type = ForgedCheckSuiteReceipt if return_subclass else CheckSuiteReceipt
        return receipt_type(**values)


class ForgedSourceCandidateReceipt(SourceCandidateReceipt):
    pass


class FakeSourceCandidateResolver:
    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls = 0
        self.requests = []

    def resolve_verified(self, request):
        self.calls += 1
        self.requests.append(request)
        values = {
            "schema_version": 1,
            "receipt_id": "46000000-0000-4000-8000-000000000001",
            "request_digest": request.request_digest,
            "repository": request.repository,
            "contract_digest": request.contract_digest,
            "contract_edition": request.contract_edition,
            "release_id": request.release_id,
            "source_commit": request.source_commit,
            "source_tree": request.source_tree,
            "embedded_manifest_hash": request.embedded_manifest_hash,
            "artifact_digest": request.artifact_digest,
            "candidate_ref": request.candidate_ref,
            "base_ref": request.base_ref,
            "check_suite_receipt_hash": request.check_suite_receipt_hash,
            "candidate_head_commit": request.source_commit,
            "candidate_head_tree": request.source_tree,
            "base_commit": "5" * 40,
            "base_tree": "6" * 40,
            "reviewed_source_commit": request.source_commit,
            "reviewed_source_tree": request.source_tree,
            "reviewed_base_commit": "5" * 40,
            "reviewed_base_tree": "6" * 40,
            "candidate_ref_reachable": True,
            "independent_review": True,
            "complete": True,
            "reviewer_identity": "candidate-reviewer",
            "verifier_identity": "candidate-verifier",
            "review_reference_hash": DIGEST_E,
            "verified_at": NOW - timedelta(seconds=5),
            "evidence_hash": DIGEST_F,
        }
        values.update(self.overrides)
        return_untyped = values.pop("return_untyped", False)
        return_subclass = values.pop("return_subclass", False)
        if return_untyped:
            return values
        receipt_type = (
            ForgedSourceCandidateReceipt if return_subclass else SourceCandidateReceipt
        )
        return receipt_type(**values)


class ForgedPilotQualificationReceipt(PilotQualificationReceipt):
    pass


def _pilot_signal_results(signal_ids, *, conclusion="success"):
    return tuple(
        PilotSignalResult(
            signal_id=signal_id,
            conclusion=conclusion,
            observed_at=NOW - timedelta(seconds=10),
            provider_reference_hash=_sha(format(index % 16, "x")),
            evidence_hash=_sha(format((index + 8) % 16, "x")),
        )
        for index, signal_id in enumerate(signal_ids)
    )


class FakePilotQualificationResolver:
    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls = 0
        self.requests = []

    def resolve_verified(self, request):
        self.calls += 1
        self.requests.append(request)
        values = {
            "schema_version": 1,
            "receipt_id": "47000000-0000-4000-8000-000000000001",
            "request_digest": request.request_digest,
            "repository": request.repository,
            "contract_digest": request.contract_digest,
            "contract_edition": request.contract_edition,
            "release_id": request.release_id,
            "pilot_operation_fingerprint": request.pilot_operation_fingerprint,
            "pilot_claim_id": request.pilot_claim_id,
            "pilot_applied_receipt_hash": request.pilot_applied_receipt_hash,
            "pilot_effect_confirmed_at": request.pilot_effect_confirmed_at,
            "artifact_digest": request.artifact_digest,
            "image_reference": request.image_reference,
            "embedded_manifest_hash": request.embedded_manifest_hash,
            "target_profile_digest": request.target_profile_digest,
            "target_snapshot_hash": request.target_snapshot_hash,
            "target_config_digest": request.target_config_digest,
            "required_signals": request.required_signals,
            "excluded_verifier_identities": (request.excluded_verifier_identities),
            "signal_results": _pilot_signal_results(request.required_signals),
            "complete": True,
            "independent_verifier": True,
            "verifier_identity": "pilot-qualification-verifier",
            "verified_at": NOW - timedelta(seconds=5),
            "external_receipt_hash": DIGEST_E,
            "evidence_hash": DIGEST_F,
        }
        values.update(self.overrides)
        return_untyped = values.pop("return_untyped", False)
        return_subclass = values.pop("return_subclass", False)
        if return_untyped:
            return values
        receipt_type = (
            ForgedPilotQualificationReceipt
            if return_subclass
            else PilotQualificationReceipt
        )
        return receipt_type(**values)


class ForgedDurableQualificationReceipt(DurableQualificationReceipt):
    pass


class FakeQualificationEvidence:
    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls = 0
        self.requests = []

    def record_durable(self, request):
        self.calls += 1
        self.requests.append(request)
        values = {
            "schema_version": 1,
            "record_id": "48000000-0000-4000-8000-000000000001",
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
            "qualification_verified_at": request.qualification_verified_at,
            "recorded_at": NOW,
            "state_version": 1,
            "durable": True,
            "evidence_hash": DIGEST_E,
        }
        values.update(self.overrides)
        return_untyped = values.pop("return_untyped", False)
        return_subclass = values.pop("return_subclass", False)
        if return_untyped:
            return values
        receipt_type = (
            ForgedDurableQualificationReceipt
            if return_subclass
            else DurableQualificationReceipt
        )
        return receipt_type(**values)


class ForgedHistoricalBuildContractReceipt(HistoricalBuildContractReceipt):
    pass


class FakeHistoricalBuildContractResolver:
    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls = 0
        self.requests = []

    def resolve_verified(self, request):
        self.calls += 1
        self.requests.append(request)
        values = {
            "schema_version": 1,
            "receipt_id": "49000000-0000-4000-8000-000000000001",
            "request_digest": request.request_digest,
            "repository": request.repository,
            "historical_build_contract_digest": (
                request.historical_build_contract_digest
            ),
            "historical_contract_edition": "historical-contract-v1",
            "previous_release_id": request.previous_release_id,
            "previous_manifest_hash": request.previous_manifest_hash,
            "source_commit": request.source_commit,
            "source_tree": request.source_tree,
            "artifact_digest": request.artifact_digest,
            "historical_required_checks": request.historical_required_checks,
            "archive_complete": True,
            "immutable": True,
            "verifier_identity": "historical-contract-verifier",
            "sealed_at": NOW - timedelta(days=30),
            "archive_reference_hash": DIGEST_D,
            "evidence_hash": DIGEST_E,
        }
        values.update(self.overrides)
        return_untyped = values.pop("return_untyped", False)
        return_subclass = values.pop("return_subclass", False)
        if return_untyped:
            return values
        receipt_type = (
            ForgedHistoricalBuildContractReceipt
            if return_subclass
            else HistoricalBuildContractReceipt
        )
        return receipt_type(**values)


class FakeRollbackResolver:
    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls = 0
        self.requests = []

    def resolve_verified(self, request):
        self.calls += 1
        self.requests.append(request)
        values = {
            "schema_version": 1,
            "receipt_id": "60000000-0000-4000-8000-000000000001",
            "request_digest": request.request_digest,
            "previous_good_digest": request.previous_good_digest,
            "previous_manifest_hash": request.previous_manifest_hash,
            "target": request.target,
            "target_snapshot_hash": request.target_snapshot_hash,
            "expected_current_digest": request.expected_current_digest,
            "current_schema": request.current_schema,
            "migration_history_digest": request.migration_history_digest,
            "can_rollback": True,
            "can_rollback_checked_at": NOW - timedelta(minutes=5),
            "artifact_retained": True,
            "retention_checked_at": NOW - timedelta(minutes=5),
            "artifact_origin": "retained",
            "runtime_attestation_valid": True,
            "runtime_reattested_at": NOW - timedelta(minutes=5),
            "pilot_operation_fingerprint": DIGEST_E,
            "source_identity": "rollback-verifier",
            "evidence_hash": DIGEST_F,
        }
        values.update(self.overrides)
        if values.pop("return_untyped", False):
            return values
        return RollbackCapabilityReceipt(**values)


class ForgedRollbackCompatibilityReceipt(RollbackCompatibilityReceipt):
    pass


class FakeRollbackCompatibilityResolver:
    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls = 0
        self.requests = []

    def resolve_verified(self, request):
        self.calls += 1
        self.requests.append(request)
        values = {
            "schema_version": 1,
            "receipt_id": "61000000-0000-4000-8000-000000000001",
            "request_digest": request.request_digest,
            "current_contract_digest": request.current_contract_digest,
            "current_contract_edition": request.current_contract_edition,
            "historical_contract_digest": request.historical_contract_digest,
            "historical_contract_edition": request.historical_contract_edition,
            "historical_contract_receipt_hash": (
                request.historical_contract_receipt_hash
            ),
            "rollback_release_id": request.rollback_release_id,
            "previous_release_id": request.previous_release_id,
            "artifact_digest": request.artifact_digest,
            "embedded_manifest_hash": request.embedded_manifest_hash,
            "migration_class": request.migration_class,
            "schema_min": request.schema_min,
            "schema_max": request.schema_max,
            "target_snapshot_hash": request.target_snapshot_hash,
            "target_config_digest": request.target_config_digest,
            "current_schema": request.current_schema,
            "migration_history_digest": request.migration_history_digest,
            "rollback_capability_receipt_hash": (
                request.rollback_capability_receipt_hash
            ),
            "compatible": True,
            "complete": True,
            "independent_verifier": True,
            "verifier_identity": "rollback-compatibility-verifier",
            "verified_at": NOW - timedelta(seconds=5),
            "external_receipt_hash": DIGEST_C,
            "evidence_hash": DIGEST_D,
        }
        values.update(self.overrides)
        return_untyped = values.pop("return_untyped", False)
        return_subclass = values.pop("return_subclass", False)
        if return_untyped:
            return values
        receipt_type = (
            ForgedRollbackCompatibilityReceipt
            if return_subclass
            else RollbackCompatibilityReceipt
        )
        return receipt_type(**values)


def _plan(
    contract,
    *,
    artifact_resolver=None,
    check_suite_resolver=None,
    source_candidate_resolver=None,
    target_resolver=None,
    manifest=None,
    clock=None,
):
    return plan_build_once_promotion(
        contract=contract,
        manifest=manifest or _manifest(contract),
        artifact_resolver=artifact_resolver or FakeArtifactResolver(),
        check_suite_resolver=check_suite_resolver or FakeCheckSuiteResolver(),
        source_candidate_resolver=(
            source_candidate_resolver or FakeSourceCandidateResolver()
        ),
        target_resolver=target_resolver or FakeTargetResolver(),
        clock=clock or FakeClock(),
    )


def _signature(key_id: str, signed_payload: bytes) -> str:
    return hashlib.sha256(key_id.encode() + b"\0" + signed_payload).hexdigest()


def _authority(operation, **overrides: object) -> AuthorityEvidence:
    signed = {
        "schema_version": 1,
        "purpose": AUTHORITY_PURPOSE,
        "audience": AUTHORITY_AUDIENCE,
        "repository": operation.repository,
        "target": operation.target.value,
        "contract_digest": operation.contract_digest,
        "contract_edition": operation.contract_edition,
        "required_checks_digest": operation.required_checks_digest,
        "authority_id": AUTHORITY_ID,
        "release_id": operation.release_id,
        "operation_fingerprint": operation.operation_fingerprint,
        "approver_identity": APPROVER,
        "runner_identity": RUNNER,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (NOW + timedelta(minutes=29)).isoformat().replace("+00:00", "Z"),
        "max_uses": 1,
        "nonce": "nonce-01",
        "key_id": "pilot-key-01",
    }
    signed.update(
        {key: value for key, value in overrides.items() if key != "signature"}
    )
    signed_bytes = canonical_json_bytes(signed)
    signed["signature"] = overrides.get(
        "signature", _signature(signed["key_id"], signed_bytes)
    )
    return AuthorityEvidence.from_mapping(signed)


class FakeAuthorityVerifier:
    def __init__(self, **overrides: object) -> None:
        self.calls = 0
        self.payloads: list[bytes] = []
        self.overrides = overrides

    def verify(self, *, key_id: str, signed_payload: bytes, signature: str):
        self.calls += 1
        self.payloads.append(signed_payload)
        payload = json.loads(signed_payload)
        values = {
            "schema_version": 1,
            "key_id": key_id,
            "authority_id": payload["authority_id"],
            "signer_identity": payload["approver_identity"],
            # A production verifier obtains this from workload OIDC, mTLS, or
            # runner attestation; it must not trust the signed payload claim.
            "runner_identity": RUNNER,
            "signed_payload_digest": canonical_sha256(payload),
            "trust_root_identity": "release-authority-root",
            "signature_valid": signature == _signature(key_id, signed_payload),
            "independent_signer": (
                payload["approver_identity"] != payload["runner_identity"]
            ),
            "evidence_hash": DIGEST_D,
        }
        values.update(self.overrides)
        if values.pop("return_untyped", False):
            return values
        return AuthorityVerificationReceipt(**values)


class FakeLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.claim_calls = 0
        self.intent_calls = 0
        self.owner_count = 0
        self.outcome_calls = 0
        self.reconcile_calls = 0
        self.pilot_read_calls = 0
        self.states: dict[str, LedgerStateSnapshot] = {}
        self.claims: dict[str, ClaimReceipt] = {}
        self.pilot_receipt: AppliedReleaseReceipt | None = None
        self.force_outcome_conflict = False
        self.force_intent_conflict = False
        self.force_reconcile_conflict = False
        self.last_claim_request: LedgerClaimRequest | None = None
        self.last_intent_request = None
        self.last_outcome_request: LedgerOutcomeRequest | None = None
        self.last_reconcile_request: ReconciliationCASRequest | None = None
        self.last_pilot_query = None
        self.negative_matrix_calls = 0
        self.negative_matrix_verified_at: datetime | None = None
        self.force_missing_negative_matrix = False
        self.negative_matrix_override: NegativeMatrixReceipt | None = None
        self.last_negative_matrix_query = None

    def pilot_applied_receipt(self, query):
        self.pilot_read_calls += 1
        self.last_pilot_query = query
        return self.pilot_receipt

    def negative_matrix_receipt(self, query):
        self.negative_matrix_calls += 1
        self.last_negative_matrix_query = query
        if self.force_missing_negative_matrix:
            return None
        if self.negative_matrix_override is not None:
            return self.negative_matrix_override
        return NegativeMatrixReceipt(
            evidence_ref=query.evidence_ref,
            repository=query.repository,
            contract_digest=query.contract_digest,
            contract_edition=query.contract_edition,
            pilot_source_ref=query.pilot_source_ref,
            production_source_ref=query.production_source_ref,
            verified_at=self.negative_matrix_verified_at or NOW,
            approver_identity="approver-negative-matrix",
            evidence_hash=DIGEST_F,
        )

    def claim_and_burn(self, request: LedgerClaimRequest) -> ClaimReceipt:
        with self._lock:
            self.claim_calls += 1
            self.last_claim_request = request
            existing = self.claims.get(request.operation_fingerprint)
            if existing is not None:
                state = self.states[request.operation_fingerprint]
                return replace(
                    existing,
                    disposition=ClaimDisposition.DUPLICATE,
                    state=state.state,
                    state_version=state.state_version,
                )
            fencing = self.owner_count + 10
            receipt = ClaimReceipt(
                disposition=ClaimDisposition.OWNER,
                external_id=request.external_id,
                authorization_id=request.authorization_id,
                operation_fingerprint=request.operation_fingerprint,
                target_key=request.target_key,
                artifact_digest=request.artifact_digest,
                operation_kind=request.operation_kind,
                contract_digest=request.contract_digest,
                evidence_hash=request.evidence_hash,
                state=OperationState.CLAIMED_LOCKED,
                state_version=1,
                fencing_token=fencing,
                durable=True,
                authority_use_burned=True,
                claim_id=CLAIM_ID,
            )
            self.owner_count += 1
            self.claims[request.operation_fingerprint] = receipt
            self.states[request.operation_fingerprint] = LedgerStateSnapshot(
                external_id=request.external_id,
                claim_id=CLAIM_ID,
                operation_fingerprint=request.operation_fingerprint,
                target=request.target_key,
                artifact_digest=request.artifact_digest,
                contract_digest=request.contract_digest,
                state=OperationState.CLAIMED_LOCKED,
                state_version=1,
                fencing_token=fencing,
                uncertainty_started_at=NOW - timedelta(minutes=2),
            )
            return receipt

    def record_provider_intent(self, request):
        with self._lock:
            self.intent_calls += 1
            self.last_intent_request = request
            state = self.states.get(request.operation_fingerprint)
            if self.force_intent_conflict or state is None:
                return None
            if (
                state.external_id != request.external_id
                or state.claim_id != request.claim_id
                or state.state is not request.expected_state
                or state.state_version != request.expected_version
                or state.fencing_token != request.fencing_token
            ):
                return None
            updated = replace(
                state,
                state_version=state.state_version + 1,
                provider_intent_hash=request.provider_intent_hash,
                provider_not_before=request.not_before,
                provider_not_after=request.not_after,
            )
            self.states[request.operation_fingerprint] = updated
            return updated

    def record_outcome(self, request: LedgerOutcomeRequest):
        with self._lock:
            self.outcome_calls += 1
            self.last_outcome_request = request
            state = self.states.get(request.operation_fingerprint)
            if self.force_outcome_conflict or state is None:
                return None
            if (
                state.state is not request.expected_state
                or state.state_version != request.expected_version
                or state.fencing_token != request.fencing_token
                or state.claim_id != request.claim_id
            ):
                return None
            updated = replace(
                state,
                state=request.next_state,
                state_version=state.state_version + 1,
            )
            self.states[request.operation_fingerprint] = updated
            if (
                request.next_state is OperationState.APPLIED
                and request.target is TargetName.PILOT
            ):
                self.pilot_receipt = AppliedReleaseReceipt(
                    receipt_id=PILOT_RECEIPT_ID,
                    claim_id=request.claim_id,
                    repository=request.repository,
                    release_id=request.release_id,
                    manifest_release_id=request.manifest_release_id,
                    operation_fingerprint=request.operation_fingerprint,
                    action=request.action,
                    target=request.target,
                    artifact_digest=request.artifact_digest,
                    embedded_manifest_hash=request.embedded_manifest_hash,
                    contract_digest=request.contract_digest,
                    contract_edition=request.contract_edition,
                    required_checks_digest=request.required_checks_digest,
                    state=OperationState.APPLIED,
                    state_version=updated.state_version,
                    fencing_token=updated.fencing_token,
                    durable=True,
                    effect_confirmed_at=NOW,
                    provider_evidence_hash=request.evidence_hash,
                )
            return updated

    def state_snapshot(self, operation_fingerprint: str):
        return self.states.get(operation_fingerprint)

    def reconcile_compare_and_swap(self, request: ReconciliationCASRequest):
        with self._lock:
            self.reconcile_calls += 1
            self.last_reconcile_request = request
            state = self.states.get(request.operation_fingerprint)
            if self.force_reconcile_conflict or state is None:
                return None
            if (
                state.state is not request.expected_state
                or state.state_version != request.expected_version
                or state.fencing_token != request.fencing_token
            ):
                return None
            updated = replace(
                state,
                state=request.next_state,
                state_version=state.state_version + 1,
            )
            self.states[request.operation_fingerprint] = updated
            outcome = self.last_outcome_request
            claim = self.claims.get(request.operation_fingerprint)
            if (
                request.next_state is OperationState.OBSERVED_APPLIED
                and state.target is TargetName.PILOT
                and outcome is not None
                and claim is not None
            ):
                self.pilot_receipt = AppliedReleaseReceipt(
                    receipt_id=PILOT_RECEIPT_ID,
                    claim_id=claim.claim_id,
                    repository=outcome.repository,
                    release_id=outcome.release_id,
                    manifest_release_id=outcome.manifest_release_id,
                    operation_fingerprint=outcome.operation_fingerprint,
                    action=outcome.action,
                    target=outcome.target,
                    artifact_digest=outcome.artifact_digest,
                    embedded_manifest_hash=outcome.embedded_manifest_hash,
                    contract_digest=outcome.contract_digest,
                    contract_edition=outcome.contract_edition,
                    required_checks_digest=outcome.required_checks_digest,
                    state=OperationState.OBSERVED_APPLIED,
                    state_version=updated.state_version,
                    fencing_token=updated.fencing_token,
                    durable=True,
                    effect_confirmed_at=NOW,
                    provider_evidence_hash=request.evidence_hash,
                )
            return updated

    def seed_uncertain(
        self,
        operation,
        *,
        state: OperationState = OperationState.OUTCOME_UNKNOWN,
        version: int = 3,
        fencing: int = 19,
        with_provider_intent: bool = True,
        intent_not_before: datetime | None = None,
        intent_not_after: datetime | None = None,
    ) -> None:
        not_before = intent_not_before or NOW - timedelta(seconds=90)
        not_after = intent_not_after or operation.evidence_valid_until
        provider_request = ProviderMutationRequest(
            operation=operation,
            image_reference=operation.image_reference,
            expected_current_artifact_digest=operation.expected_current_digest,
            expected_target_config_digest=operation.target_config_digest,
            expected_current_schema=operation.current_schema,
            expected_migration_history_digest=operation.migration_history_digest,
            target_profile_digest=operation.target_profile_digest,
            target_snapshot_hash=operation.target_snapshot_hash,
            external_id=operation_external_id(operation.operation_fingerprint),
            claim_id=CLAIM_ID,
            fencing_token=fencing,
            idempotency_key=provider_idempotency_key(operation.operation_fingerprint),
            not_before=not_before,
            not_after=not_after,
        )
        self.states[operation.operation_fingerprint] = LedgerStateSnapshot(
            external_id=operation_external_id(operation.operation_fingerprint),
            claim_id=CLAIM_ID,
            operation_fingerprint=operation.operation_fingerprint,
            target=operation.target,
            artifact_digest=operation.artifact_digest,
            contract_digest=operation.contract_digest,
            state=state,
            state_version=version,
            fencing_token=fencing,
            uncertainty_started_at=NOW - timedelta(minutes=2),
            provider_intent_hash=(
                provider_request.intent_hash if with_provider_intent else None
            ),
            provider_not_before=not_before if with_provider_intent else None,
            provider_not_after=not_after if with_provider_intent else None,
        )


class ClaimTamperingLedger(FakeLedger):
    def __init__(self, **overrides: object) -> None:
        super().__init__()
        self.overrides = overrides

    def claim_and_burn(self, request: LedgerClaimRequest) -> ClaimReceipt:
        receipt = super().claim_and_burn(request)
        return replace(receipt, **self.overrides)


class IntentTamperingLedger(FakeLedger):
    def __init__(self, **overrides: object) -> None:
        super().__init__()
        self.overrides = overrides

    def record_provider_intent(self, request):
        snapshot = super().record_provider_intent(request)
        if snapshot is None:
            return None
        return replace(snapshot, **self.overrides)


class OutcomeTamperingLedger(FakeLedger):
    def __init__(self, **overrides: object) -> None:
        super().__init__()
        self.overrides = overrides

    def record_outcome(self, request: LedgerOutcomeRequest):
        snapshot = super().record_outcome(request)
        if snapshot is None:
            return None
        return replace(snapshot, **self.overrides)


class ReconcileTamperingLedger(FakeLedger):
    def __init__(self, **overrides: object) -> None:
        super().__init__()
        self.overrides = overrides

    def reconcile_compare_and_swap(self, request: ReconciliationCASRequest):
        snapshot = super().reconcile_compare_and_swap(request)
        if snapshot is None:
            return None
        return replace(snapshot, **self.overrides)


class LostOutcomeReplyLedger(FakeLedger):
    def __init__(self) -> None:
        super().__init__()
        self.reply_lost = False

    def record_outcome(self, request: LedgerOutcomeRequest):
        snapshot = super().record_outcome(request)
        if (
            snapshot is not None
            and request.next_state is OperationState.APPLIED
            and not self.reply_lost
        ):
            self.reply_lost = True
            raise TimeoutError("outcome reply lost after durable CAS")
        return snapshot


class LostReconciliationReplyLedger(FakeLedger):
    def __init__(self) -> None:
        super().__init__()
        self.reply_lost = False

    def reconcile_compare_and_swap(self, request: ReconciliationCASRequest):
        snapshot = super().reconcile_compare_and_swap(request)
        if snapshot is not None and not self.reply_lost:
            self.reply_lost = True
            raise TimeoutError("reconciliation reply lost after durable CAS")
        return snapshot


class RacingDuplicateEvidenceLedger(FakeLedger):
    """Model another valid claimant winning between state lookup and claim."""

    def __init__(self, operation: ReleaseOperation) -> None:
        super().__init__()
        self.operation = operation
        self.snapshot_calls = 0

    def claim_and_burn(self, request: LedgerClaimRequest) -> ClaimReceipt:
        if request.operation_fingerprint not in self.claims:
            winner_request = replace(
                request,
                authorization_id="10000000-0000-4000-8000-000000000002",
                evidence_hash=DIGEST_A,
            )
            super().claim_and_burn(winner_request)
        return super().claim_and_burn(request)

    def state_snapshot(self, operation_fingerprint: str):
        self.snapshot_calls += 1
        snapshot = super().state_snapshot(operation_fingerprint)
        if (
            self.snapshot_calls == 2
            and snapshot is not None
            and snapshot.state is OperationState.CLAIMED_LOCKED
        ):
            not_before = NOW - timedelta(seconds=1)
            provider_request = ProviderMutationRequest(
                operation=self.operation,
                image_reference=self.operation.image_reference,
                expected_current_artifact_digest=(
                    self.operation.expected_current_digest
                ),
                expected_target_config_digest=self.operation.target_config_digest,
                expected_current_schema=self.operation.current_schema,
                expected_migration_history_digest=(
                    self.operation.migration_history_digest
                ),
                target_profile_digest=self.operation.target_profile_digest,
                target_snapshot_hash=self.operation.target_snapshot_hash,
                external_id=snapshot.external_id,
                claim_id=snapshot.claim_id,
                fencing_token=snapshot.fencing_token,
                idempotency_key=provider_idempotency_key(
                    self.operation.operation_fingerprint
                ),
                not_before=not_before,
                not_after=self.operation.evidence_valid_until,
            )
            self.states[operation_fingerprint] = replace(
                snapshot,
                state=OperationState.APPLIED,
                state_version=snapshot.state_version + 2,
                provider_intent_hash=provider_request.intent_hash,
                provider_not_before=provider_request.not_before,
                provider_not_after=provider_request.not_after,
            )
            snapshot = self.states[operation_fingerprint]
        return snapshot


class ForgedProviderApplyResult(ProviderApplyResult):
    pass


class FakeProvider:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        result_overrides: dict[str, object] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.failure = failure
        self.result_overrides = result_overrides or {}
        self.keys: list[str] = []
        self.requests: list[ProviderMutationRequest] = []
        self.results: list[ProviderApplyResult] = []

    def apply(self, request: ProviderMutationRequest):
        with self._lock:
            self.calls += 1
            self.keys.append(request.idempotency_key)
            self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        operation = request.operation
        values = {
            "operation_fingerprint": operation.operation_fingerprint,
            "external_id": request.external_id,
            "claim_id": request.claim_id,
            "idempotency_key": request.idempotency_key,
            "fencing_token": request.fencing_token,
            "not_before": request.not_before,
            "not_after": request.not_after,
            "applied_at": request.not_before,
            "observed_previous_artifact_digest": operation.expected_current_digest,
            "observed_target_config_digest": operation.target_config_digest,
            "observed_current_schema": operation.current_schema,
            "observed_migration_history_digest": (operation.migration_history_digest),
            "artifact_digest": operation.artifact_digest,
            "image_reference": request.image_reference,
            "target_profile_digest": operation.target_profile_digest,
            "target_snapshot_hash": operation.target_snapshot_hash,
            "provider_reference_hash": DIGEST_E,
            "evidence_hash": DIGEST_F,
        }
        values.update(self.result_overrides)
        return_untyped = values.pop("return_untyped", False)
        return_subclass = values.pop("return_subclass", False)
        if return_untyped:
            return values
        result_type = (
            ForgedProviderApplyResult if return_subclass else ProviderApplyResult
        )
        result = result_type(**values)
        self.results.append(result)
        return result


class FakeObserver:
    def __init__(self, envelopes) -> None:
        self.envelopes = envelopes
        self.calls = 0
        self.uncertainty_started_at = None

    def observe_verified(self, operation, uncertainty_started_at):
        del operation
        self.calls += 1
        self.uncertainty_started_at = uncertainty_started_at
        return self.envelopes


def _pilot_receipt(operation, **overrides: object) -> AppliedReleaseReceipt:
    is_rollback = operation.action is ReleaseAction.ROLLBACK
    values = {
        "receipt_id": PILOT_RECEIPT_ID,
        "claim_id": CLAIM_ID,
        "repository": operation.repository,
        "release_id": operation.manifest_release_id,
        "manifest_release_id": operation.manifest_release_id,
        "operation_fingerprint": (operation.required_pilot_operation_fingerprint),
        "action": ReleaseAction.PROMOTE,
        "target": TargetName.PILOT,
        "artifact_digest": operation.artifact_digest,
        "embedded_manifest_hash": operation.embedded_manifest_hash,
        "contract_digest": (
            operation.rollback_historical_contract_digest
            if is_rollback
            else operation.contract_digest
        ),
        "contract_edition": (
            operation.rollback_historical_contract_edition
            if is_rollback
            else operation.contract_edition
        ),
        "required_checks_digest": (
            operation.rollback_historical_required_checks_digest
            if is_rollback
            else operation.required_checks_digest
        ),
        "state": OperationState.APPLIED,
        "state_version": 2,
        "fencing_token": 10,
        "durable": True,
        "effect_confirmed_at": NOW - timedelta(seconds=20),
        "provider_evidence_hash": DIGEST_F,
    }
    values.update(overrides)
    return AppliedReleaseReceipt(**values)


def _qualified_production(
    contract,
    *,
    plan=None,
    ledger=None,
    qualification_resolver=None,
    qualification_evidence=None,
    target_resolver=None,
    clock=None,
):
    promotion_plan = plan or _plan(contract)
    qualification_ledger = ledger
    if qualification_ledger is None:
        qualification_ledger = FakeLedger()
        qualification_ledger.pilot_receipt = _pilot_receipt(
            promotion_plan.operations[1]
        )
    return finalize_production_operation(
        contract=contract,
        plan=promotion_plan,
        ledger=qualification_ledger,
        qualification_resolver=(
            qualification_resolver or FakePilotQualificationResolver()
        ),
        qualification_evidence=(qualification_evidence or FakeQualificationEvidence()),
        target_resolver=target_resolver or FakeTargetResolver(),
        clock=clock or FakeClock(),
    )


def _execute(
    *,
    contract,
    operation,
    ledger=None,
    provider=None,
    authority=None,
    clock=None,
    verifier=None,
):
    return execute_operation(
        contract=contract,
        operation=operation,
        authority=authority or _authority(operation),
        clock=clock or FakeClock(),
        authority_verifier=verifier or FakeAuthorityVerifier(),
        ledger=ledger or FakeLedger(),
        provider=provider or FakeProvider(),
    )


def _observation(operation, *, reference=DIGEST_A, **overrides: object):
    values = {
        "operation_fingerprint": operation.operation_fingerprint,
        "external_id": operation_external_id(operation.operation_fingerprint),
        "claim_id": CLAIM_ID,
        "idempotency_key": provider_idempotency_key(operation.operation_fingerprint),
        "fencing_token": 19,
        "not_before": NOW - timedelta(seconds=90),
        "not_after": operation.evidence_valid_until,
        "applied_at": NOW - timedelta(seconds=60),
        "observed_previous_artifact_digest": operation.expected_current_digest,
        "observed_target_config_digest": operation.target_config_digest,
        "observed_current_schema": operation.current_schema,
        "observed_migration_history_digest": operation.migration_history_digest,
        "artifact_digest": operation.artifact_digest,
        "image_reference": operation.image_reference,
        "target_profile_digest": operation.target_profile_digest,
        "target_snapshot_hash": operation.target_snapshot_hash,
        "provider_reference_hash": reference,
        "evidence_hash": DIGEST_F,
    }
    values.update(overrides)
    return ProviderObservation(**values)


def _envelope(
    operation,
    index: int,
    *,
    captured_at: datetime | None = None,
    observations=(),
    **overrides: object,
):
    captured = captured_at or NOW - timedelta(seconds=10)
    values = {
        "schema_version": 1,
        "snapshot_id": f"70000000-0000-4000-8000-{index:012d}",
        "source_identity": "provider-observer",
        "operation_fingerprint": operation.operation_fingerprint,
        "target": operation.target,
        "target_profile_digest": operation.target_profile_digest,
        "complete": True,
        "captured_at": captured,
        "settled_through": captured - timedelta(seconds=1),
        "observations": tuple(observations),
        "evidence_hash": _sha(format(index % 16, "x")),
    }
    values.update(overrides)
    return ObservationEnvelope(**values)


def test_contract_and_manifest_are_bound_by_exact_machine_check_ids() -> None:
    contract = load_release_control_contract(CONTRACT_PATH)
    plan = _plan(contract)

    assert contract.cutover_enabled is False
    assert plan.contract_digest == contract.contract_digest
    assert plan.contract_edition == contract.contract_edition
    assert plan.required_checks == contract.required_checks
    assert plan.operations[0].required_checks == contract.required_checks


def test_contract_rejects_extra_or_missing_fields() -> None:
    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    payload["artifact"]["unexpected"] = True
    with pytest.raises(ContractError, match="extra"):
        validate_release_control_contract(payload)

    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    del payload["rollback"]["require_can_rollback"]
    with pytest.raises(ContractError, match="missing"):
        validate_release_control_contract(payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("authority", "purpose", "other-purpose"),
        ("authority", "audience", "other-audience"),
        ("authority", "required_signed_fields", ["operation_fingerprint"]),
        ("ledger", "target_version_cas_required", False),
        ("ledger", "raw_evidence_forbidden", False),
        ("ledger", "claim_field_allowlist", ["operation_fingerprint"]),
        ("promotion", "provider_idempotency_required", False),
        ("promotion", "provider_fencing_required", False),
        ("promotion", "provider_deadline_required", False),
        ("promotion", "check_suite_receipt_required", False),
        ("promotion", "check_suite_max_age_seconds", 0),
        ("promotion", "source_candidate_receipt_required", False),
        ("promotion", "independent_review_required", False),
        ("promotion", "pilot_qualification_receipt_required", False),
        ("promotion", "pilot_qualification_max_age_seconds", 0),
        ("promotion", "pilot_qualification_independent_verifier_required", False),
        ("promotion", "pilot_qualification_required_signals", ["readiness"]),
        ("promotion", "zero_settlement_observations", 1),
        ("promotion", "zero_settlement_min_interval_seconds", 0),
        ("promotion", "reconciliation_settling_seconds", 0),
        ("rollback", "historical_build_contract_receipt_required", False),
        ("rollback", "compatibility_receipt_required", False),
        ("rollback", "compatibility_max_age_seconds", 0),
        ("rollback", "independent_verifier_required", False),
    ],
)
def test_contract_requires_explicit_authority_ledger_and_provider_safety(
    section,
    field,
    value,
) -> None:
    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    payload[section][field] = value

    with pytest.raises(ContractError):
        validate_release_control_contract(payload)


def test_validated_contract_cannot_be_cloned_to_bypass_cutover() -> None:
    contract = _contract(cutover=False)

    with pytest.raises(ContractError, match="strict repository validation"):
        replace(contract, cutover_enabled=True)

    values = {
        item.name: getattr(contract, item.name)
        for item in fields(ReleaseControlContract)
        if not item.name.startswith("_")
    }
    values["cutover_enabled"] = True
    with pytest.raises(ContractError, match="strict repository validation"):
        ReleaseControlContract(**values)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("repository", "owner"), "Owner"),
        (("repository", "owner"), ".."),
        (("source_refs", "pilot"), "topic/.hidden"),
        (("source_refs", "pilot"), "topic/release.lock"),
        (("source_refs", "pilot"), "x" * 256),
    ],
)
def test_contract_rejects_noncanonical_bounded_references(path, value) -> None:
    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    section, field = path
    payload[section][field] = value

    with pytest.raises(ContractError):
        validate_release_control_contract(payload)


def test_contract_rejects_unsafe_or_overlapping_forbidden_paths() -> None:
    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    payload["forbidden_paths"] = ["../sitecustomize.py"]
    with pytest.raises(ContractError, match="relative"):
        validate_release_control_contract(payload)

    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    payload["forbidden_paths"] = [payload["protected_paths"][0]]
    with pytest.raises(ContractError, match="overlap"):
        validate_release_control_contract(payload)


def test_contract_protected_path_capacity_supports_tcb_growth() -> None:
    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    payload["protected_paths"] = sorted(
        ["scripts/release_control.py"]
        + [f"protected/path-{index:03d}" for index in range(255)]
    )

    contract = validate_release_control_contract(payload)

    assert len(contract.protected_paths) == 256

    payload["protected_paths"].append("protected/path-over-cap")
    payload["protected_paths"].sort()
    with pytest.raises(ContractError, match="1..256"):
        validate_release_control_contract(payload)


def test_manifest_schema_version_and_contract_checks_are_literal() -> None:
    contract = _contract(cutover=False)
    with pytest.raises(ReleaseManifestError, match="must equal 1"):
        _manifest(contract, schema_version=2)

    mismatched = _manifest(contract, required_checks=[contract.required_checks[0]])
    with pytest.raises(SchemaError, match="exactly match"):
        _plan(contract, manifest=mismatched)


def test_resolver_receipt_between_trusted_clock_samples_is_not_future() -> None:
    contract = _contract(cutover=False)
    resolved_at = NOW + timedelta(seconds=1)
    clock = SequenceClock(
        NOW,
        NOW + timedelta(seconds=2),
        *([NOW + timedelta(seconds=2)] * 8),
    )

    plan = _plan(
        contract,
        artifact_resolver=FakeArtifactResolver(resolved_at=resolved_at),
        clock=clock,
    )

    assert plan._artifact.resolved_at == resolved_at
    assert clock.calls == 10


def test_resolver_window_rejects_a_backwards_trusted_clock() -> None:
    contract = _contract(cutover=False)
    artifact_resolver = FakeArtifactResolver()

    with pytest.raises(EvidenceRejected, match="moved backwards"):
        _plan(
            contract,
            artifact_resolver=artifact_resolver,
            clock=SequenceClock(NOW, NOW - timedelta(microseconds=1)),
        )

    assert artifact_resolver.calls == 1


def test_planning_rejects_a_backward_jump_between_resolver_windows() -> None:
    contract = _contract(cutover=False)
    check_suite_resolver = FakeCheckSuiteResolver()

    with pytest.raises(EvidenceRejected, match="across release stages"):
        _plan(
            contract,
            check_suite_resolver=check_suite_resolver,
            clock=SequenceClock(
                NOW,
                NOW + timedelta(seconds=10),
                NOW - timedelta(seconds=100),
            ),
        )

    assert check_suite_resolver.calls == 0


def test_manifest_from_another_contract_is_rejected_even_with_same_checks() -> None:
    contract = _contract(cutover=False)
    foreign = _manifest(contract, build_contract_digest=DIGEST_A)

    with pytest.raises(SchemaError, match="build contract digest"):
        _plan(contract, manifest=foreign)


@pytest.mark.parametrize("migration_class", [item.value for item in MigrationClass])
def test_all_literal_migration_classes_are_planned(migration_class) -> None:
    contract = _contract(cutover=False)
    schema_range = (
        {"schema_min": 0, "schema_max": 0}
        if migration_class == MigrationClass.NONE.value
        else {}
    )
    manifest = _manifest(
        contract,
        migration_class=migration_class,
        **schema_range,
    )

    target_resolver = (
        FakeTargetResolver(current_schema=0)
        if migration_class == MigrationClass.NONE.value
        else None
    )
    operation = _plan(
        contract,
        manifest=manifest,
        target_resolver=target_resolver,
    ).operations[0]

    assert operation.migration_class.value == migration_class


@pytest.mark.parametrize(
    ("migration_class", "schema_min", "schema_max"),
    [
        (MigrationClass.NONE, 1, 1),
        (MigrationClass.EXPAND, 0, 0),
    ],
)
def test_runner_revalidates_forged_migration_semantics(
    migration_class: MigrationClass,
    schema_min: int,
    schema_max: int,
) -> None:
    contract = _contract(cutover=False)
    forged = replace(
        _manifest(contract),
        migration_class=migration_class,
        schema_min=schema_min,
        schema_max=schema_max,
    )

    with pytest.raises(SchemaError, match="canonical revalidation"):
        _plan(contract, manifest=forged)


def test_manifest_rejects_self_and_final_digest_fields() -> None:
    contract = _contract(cutover=False)
    payload = _manifest(contract).to_mapping()
    for field in ("manifest_hash", "artifact_digest"):
        invalid = dict(payload)
        invalid[field] = DIGEST_A
        with pytest.raises(ReleaseManifestError):
            EmbeddedManifest.from_mapping(invalid)


def test_release_manifest_helpers_are_reexported_without_second_schema() -> None:
    assert canonical_sha256({"b": 2, "a": 1}) == (
        "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )
    with pytest.raises(ReleaseManifestError, match="duplicate JSON key"):
        loads_strict_json('{"a":1,"a":2}')


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"media_type": OCI_INDEX_MEDIA_TYPE}, "media_type"),
        ({"digest_kind": "oci_index"}, "digest_kind"),
        ({"platform_architecture": "arm64"}, "platform_architecture"),
        (
            {"image_reference": f"registry.example.invalid/team/app:latest@{DIGEST_A}"},
            "mutable tag",
        ),
        (
            {"image_reference": f"https://registry.example.invalid/app@{DIGEST_A}"},
            "digest-only",
        ),
        ({"embedded_content_hash": DIGEST_B}, "embedded_content_hash"),
        ({"request_digest": DIGEST_B}, "request_digest"),
        ({"contract_digest": DIGEST_B}, "contract_digest"),
        ({"provenance": "retained"}, "provenance"),
        ({"resolved_at": NOW - timedelta(seconds=901)}, "stale"),
        ({"return_untyped": True}, "untyped"),
    ],
)
def test_unverified_or_mismatched_artifact_receipts_cannot_plan(
    overrides, message
) -> None:
    contract = _contract(cutover=False)
    with pytest.raises(EvidenceRejected, match=message):
        _plan(contract, artifact_resolver=FakeArtifactResolver(**overrides))


def test_artifact_receipt_must_use_the_contract_oci_repository() -> None:
    contract = _contract(cutover=False)
    resolver = FakeArtifactResolver(
        image_reference=f"registry.example.invalid/other/app@{DIGEST_A}"
    )

    with pytest.raises(EvidenceRejected, match="contract OCI repository"):
        _plan(contract, artifact_resolver=resolver)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"request_digest": DIGEST_A}, "request_digest"),
        ({"repository": "other/repository"}, "repository"),
        ({"source_commit": "3" * 40}, "source_commit"),
        ({"source_tree": "4" * 40}, "source_tree"),
        ({"embedded_manifest_hash": DIGEST_B}, "embedded_manifest_hash"),
        ({"artifact_digest": DIGEST_B}, "artifact_digest"),
        ({"contract_digest": DIGEST_B}, "contract_digest"),
        ({"complete": False}, "complete"),
        ({"forbidden_paths_absent": False}, "forbidden_paths_absent"),
        ({"verified_at": NOW - timedelta(seconds=3601)}, "stale"),
        ({"return_untyped": True}, "untyped"),
        ({"return_subclass": True}, "untyped"),
    ],
)
def test_check_suite_receipt_is_exact_typed_and_fresh(overrides, message) -> None:
    contract = _contract(cutover=False)

    with pytest.raises(EvidenceRejected, match=message):
        _plan(
            contract,
            check_suite_resolver=FakeCheckSuiteResolver(**overrides),
        )


@pytest.mark.parametrize("result_kind", ["missing", "extra", "duplicate", "failed"])
def test_check_suite_requires_exactly_one_success_per_required_check(
    result_kind,
) -> None:
    contract = _contract(cutover=False)
    check_ids = list(contract.required_checks)
    if result_kind == "missing":
        results = _check_results(check_ids[:-1])
    elif result_kind == "extra":
        results = _check_results([*check_ids, "zz-extra-check"])
    elif result_kind == "duplicate":
        results = _check_results([check_ids[0], *check_ids])
    else:
        results = _check_results(check_ids, conclusion="failure")

    with pytest.raises(EvidenceRejected):
        _plan(
            contract,
            check_suite_resolver=FakeCheckSuiteResolver(results=results),
        )


def test_check_suite_rejects_fresh_wrapper_around_stale_or_mixed_run_results() -> None:
    contract = _contract(cutover=False)
    stale_results = tuple(
        replace(
            result,
            completed_at=NOW - timedelta(seconds=3601),
        )
        for result in _check_results(contract.required_checks)
    )
    with pytest.raises(EvidenceRejected, match="stale result"):
        _plan(
            contract,
            check_suite_resolver=FakeCheckSuiteResolver(results=stale_results),
        )

    mixed_results = list(_check_results(contract.required_checks))
    mixed_results[-1] = replace(
        mixed_results[-1],
        suite_reference_hash=DIGEST_D,
    )
    with pytest.raises(EvidenceRejected, match="different suites"):
        _plan(
            contract,
            check_suite_resolver=FakeCheckSuiteResolver(results=tuple(mixed_results)),
        )


def test_check_suite_receipt_hash_is_bound_into_both_operations() -> None:
    contract = _contract(cutover=False)
    first = _plan(contract)
    second = _plan(
        contract,
        check_suite_resolver=FakeCheckSuiteResolver(evidence_hash=DIGEST_A),
    )

    assert first.operations[0].check_suite_receipt_hash == (
        first.operations[1].check_suite_receipt_hash
    )
    assert first.operations[0].operation_fingerprint != (
        second.operations[0].operation_fingerprint
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"request_digest": DIGEST_A}, "request_digest"),
        ({"source_commit": "3" * 40}, "source_commit"),
        ({"source_tree": "4" * 40}, "source_tree"),
        ({"artifact_digest": DIGEST_B}, "artifact_digest"),
        ({"candidate_ref": "other-candidate"}, "candidate_ref"),
        ({"base_ref": "other-base"}, "base_ref"),
        ({"candidate_head_commit": "3" * 40}, "candidate_head_commit"),
        ({"reviewed_source_commit": "3" * 40}, "reviewed_source_commit"),
        ({"reviewed_base_commit": "7" * 40}, "base snapshot"),
        ({"reviewed_base_tree": "8" * 40}, "base snapshot"),
        ({"candidate_ref_reachable": False}, "candidate_ref_reachable"),
        ({"independent_review": False}, "independent_review"),
        ({"complete": False}, "complete"),
        ({"reviewer_identity": "candidate-verifier"}, "independent"),
        ({"reviewer_identity": "check-suite-verifier"}, "independent"),
        ({"verified_at": NOW - timedelta(seconds=3601)}, "stale"),
        ({"return_untyped": True}, "untyped"),
        ({"return_subclass": True}, "untyped"),
    ],
)
def test_source_candidate_receipt_is_exact_independent_typed_and_fresh(
    overrides,
    message,
) -> None:
    contract = _contract(cutover=False)

    with pytest.raises(EvidenceRejected, match=message):
        _plan(
            contract,
            source_candidate_resolver=FakeSourceCandidateResolver(**overrides),
        )


def test_source_candidate_receipt_is_bound_into_both_promotion_operations() -> None:
    contract = _contract(cutover=False)
    first = _plan(contract)
    second = _plan(
        contract,
        source_candidate_resolver=FakeSourceCandidateResolver(evidence_hash=DIGEST_A),
    )

    assert first.operations[0].source_candidate_receipt_hash == (
        first.operations[1].source_candidate_receipt_hash
    )
    assert first.operations[0].operation_fingerprint != (
        second.operations[0].operation_fingerprint
    )


def test_artifact_descriptor_cannot_be_built_from_raw_caller_assertions() -> None:
    with pytest.raises(EvidenceRejected, match="verified resolver"):
        ArtifactDescriptor()
    assert not hasattr(ArtifactDescriptor, "from_mapping")


def test_embedded_layer_receipt_is_bound_into_operation_fingerprint() -> None:
    contract = _contract(cutover=False)
    first = _plan(contract).operations[0]
    second = _plan(
        contract,
        artifact_resolver=FakeArtifactResolver(embedded_layer_digest=DIGEST_C),
    ).operations[0]

    assert first.embedded_layer_digest != second.embedded_layer_digest
    assert first.operation_fingerprint != second.operation_fingerprint


@pytest.mark.parametrize(
    "reference",
    [
        f"registry.example.invalid:5000/team/app@{DIGEST_A}",
        f"registry.example.invalid/team/app@{DIGEST_A}",
    ],
)
def test_oci_parser_accepts_only_bounded_digest_references(reference) -> None:
    assert validate_immutable_image_reference(reference, DIGEST_A) == reference


@pytest.mark.parametrize(
    "reference",
    [
        f"registry.example.invalid/team/app:tag@{DIGEST_A}",
        f"registry.example.invalid:99999/team/app@{DIGEST_A}",
        f"Registry.example.invalid/team/app@{DIGEST_A}",
        f"registry.example.invalid//app@{DIGEST_A}",
        f"registry.example.invalid/team/app@{DIGEST_B}",
        "r" * 513,
    ],
)
def test_oci_parser_rejects_tags_bad_ports_case_gaps_mismatch_and_overflow(
    reference,
) -> None:
    with pytest.raises(SchemaError):
        validate_immutable_image_reference(reference, DIGEST_A)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"request_digest": DIGEST_A}, "request_digest"),
        ({"candidate_artifact_digest": DIGEST_B}, "candidate_artifact_digest"),
        ({"candidate_manifest_hash": DIGEST_B}, "candidate_manifest_hash"),
        ({"target": TargetName.PRODUCTION}, "target"),
        ({"complete": False}, "compatible"),
        ({"schema_compatible": False}, "compatible"),
        ({"migration_allowed": False}, "compatible"),
        ({"current_schema": 7}, "current_schema"),
        ({"captured_at": NOW - timedelta(seconds=301)}, "stale"),
        ({"return_untyped": True}, "untyped"),
    ],
)
def test_target_schema_and_migration_receipts_fail_closed(overrides, message) -> None:
    contract = _contract(cutover=False)
    with pytest.raises(EvidenceRejected, match=message):
        _plan(contract, target_resolver=FakeTargetResolver(**overrides))


def test_target_snapshot_hash_schema_and_history_are_bound_into_operation() -> None:
    contract = _contract(cutover=False)
    first = _plan(contract).operations[0]
    second = _plan(
        contract,
        target_resolver=FakeTargetResolver(migration_history_digest=DIGEST_A),
    ).operations[0]

    assert first.current_schema == 5
    assert first.migration_history_digest == DIGEST_E
    assert first.operation_fingerprint != second.operation_fingerprint


def test_build_once_plan_uses_one_exact_digest_for_pilot_and_production() -> None:
    contract = _contract(cutover=False)
    plan = _plan(contract)

    assert plan.artifact_build_count == 1
    assert tuple(item.target for item in plan.operations) == (
        TargetName.PILOT,
        TargetName.PRODUCTION,
    )
    assert {item.artifact_digest for item in plan.operations} == {DIGEST_A}
    assert plan.operations[0].required_pilot_operation_fingerprint is None
    assert plan.operations[1].required_pilot_operation_fingerprint == (
        plan.operations[0].operation_fingerprint
    )
    assert all(
        operation.qualified_pilot_applied_receipt_hash is None
        for operation in plan.operations
    )
    repeated = _plan(contract)
    assert tuple(item.operation_fingerprint for item in plan.operations) == tuple(
        item.operation_fingerprint for item in repeated.operations
    )


def test_unqualified_production_draft_cannot_execute() -> None:
    contract = _contract(cutover=True)
    draft = _plan(contract).operations[1]
    ledger = FakeLedger()
    provider = FakeProvider()

    with pytest.raises(EvidenceRejected, match="qualification"):
        _execute(
            contract=contract,
            operation=draft,
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_production_finalizer_revalidates_sealed_plan_linkage() -> None:
    contract = _contract(cutover=True)
    plan = _plan(contract)
    object.__setattr__(plan, "artifact_digest", DIGEST_B)

    with pytest.raises(EvidenceRejected, match="sealed evidence linkage"):
        _qualified_production(contract, plan=plan)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"request_digest": DIGEST_A}, "request_digest"),
        ({"pilot_applied_receipt_hash": DIGEST_A}, "receipt_hash"),
        (
            {
                "artifact_digest": DIGEST_B,
                "image_reference": (f"registry.example.invalid/team/app@{DIGEST_B}"),
            },
            "artifact_digest",
        ),
        ({"target_config_digest": DIGEST_B}, "target_config_digest"),
        ({"complete": False}, "complete"),
        ({"independent_verifier": False}, "independent_verifier"),
        ({"verifier_identity": "candidate-reviewer"}, "not independent"),
        ({"verifier_identity": "artifact-verifier"}, "not independent"),
        ({"verifier_identity": "target-state-verifier"}, "not independent"),
        ({"verified_at": NOW - timedelta(seconds=301)}, "stale"),
        ({"return_untyped": True}, "untyped"),
        ({"return_subclass": True}, "untyped"),
    ],
)
def test_pilot_qualification_receipt_is_exact_typed_fresh_and_independent(
    overrides,
    message,
) -> None:
    contract = _contract(cutover=True)

    with pytest.raises(EvidenceRejected, match=message):
        _qualified_production(
            contract,
            qualification_resolver=FakePilotQualificationResolver(**overrides),
        )


@pytest.mark.parametrize("result_kind", ["missing", "extra", "duplicate", "failed"])
def test_pilot_qualification_requires_exact_successful_signals(result_kind) -> None:
    contract = _contract(cutover=True)
    signal_ids = list(contract.pilot_qualification_required_signals)
    if result_kind == "missing":
        results = _pilot_signal_results(signal_ids[:-1])
    elif result_kind == "extra":
        results = _pilot_signal_results([*signal_ids, "zz-extra-signal"])
    elif result_kind == "duplicate":
        results = _pilot_signal_results([signal_ids[0], *signal_ids])
    else:
        results = _pilot_signal_results(signal_ids, conclusion="failure")

    with pytest.raises(EvidenceRejected):
        _qualified_production(
            contract,
            qualification_resolver=FakePilotQualificationResolver(
                signal_results=results
            ),
        )


@pytest.mark.parametrize(
    "state",
    [
        OperationState.OUTCOME_UNKNOWN,
        OperationState.OBSERVED_NOT_APPLIED,
        OperationState.MANUAL_REVIEW,
    ],
)
def test_non_applied_reconciliation_states_cannot_qualify_production(state) -> None:
    contract = _contract(cutover=True)
    plan = _plan(contract)
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(plan.operations[1], state=state)

    with pytest.raises(LedgerSafetyError, match="applied effect"):
        _qualified_production(contract, plan=plan, ledger=ledger)


def test_durable_observed_applied_pilot_can_be_qualified_without_retry() -> None:
    contract = _contract(cutover=True)
    plan = _plan(contract)
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(
        plan.operations[1],
        state=OperationState.OBSERVED_APPLIED,
    )

    operation = _qualified_production(contract, plan=plan, ledger=ledger)

    assert operation.pilot_qualification_receipt_hash is not None
    assert operation.qualified_pilot_applied_receipt_hash == (
        ledger.pilot_receipt.receipt_hash
    )


def test_pilot_qualification_receipt_hash_changes_production_fingerprint() -> None:
    contract = _contract(cutover=True)
    plan = _plan(contract)
    first = _qualified_production(contract, plan=plan)
    second = _qualified_production(
        contract,
        plan=plan,
        qualification_resolver=FakePilotQualificationResolver(evidence_hash=DIGEST_A),
    )

    assert first.pilot_qualification_receipt_hash != (
        second.pilot_qualification_receipt_hash
    )
    assert first.operation_fingerprint != second.operation_fingerprint


def test_production_execution_requires_the_pilot_receipt_that_was_qualified() -> None:
    contract = _contract(cutover=True)
    plan = _plan(contract)
    ledger = FakeLedger()
    qualified_receipt = _pilot_receipt(
        plan.operations[1],
        effect_confirmed_at=NOW - timedelta(seconds=20),
    )
    ledger.pilot_receipt = qualified_receipt
    operation = _qualified_production(contract, plan=plan, ledger=ledger)
    provider = FakeProvider()

    assert operation.qualified_pilot_applied_receipt_hash == (
        qualified_receipt.receipt_hash
    )
    ledger.pilot_receipt = replace(
        qualified_receipt,
        receipt_id="30000000-0000-4000-8000-000000000002",
        claim_id="20000000-0000-4000-8000-000000000002",
        effect_confirmed_at=NOW,
    )

    with pytest.raises(LedgerSafetyError, match="qualified receipt"):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_pilot_qualification_rejects_fresh_wrapper_around_stale_signal() -> None:
    contract = _contract(cutover=True)
    plan = _plan(contract)
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(
        plan.operations[1],
        effect_confirmed_at=NOW - timedelta(minutes=10),
    )
    stale_results = tuple(
        replace(
            result,
            observed_at=NOW - timedelta(seconds=301),
        )
        for result in _pilot_signal_results(
            contract.pilot_qualification_required_signals
        )
    )

    with pytest.raises(EvidenceRejected, match="stale signal"):
        _qualified_production(
            contract,
            plan=plan,
            ledger=ledger,
            qualification_resolver=FakePilotQualificationResolver(
                signal_results=stale_results
            ),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_digest": DIGEST_A},
        {"pilot_claim_id": "20000000-0000-4000-8000-000000000002"},
        {"qualification_receipt_hash": DIGEST_A},
        {"external_receipt_hash": DIGEST_A},
        {"artifact_digest": DIGEST_B},
        {"verifier_identity": "different-verifier"},
        {"durable": False},
        {"recorded_at": NOW + timedelta(seconds=1)},
        {"state_version": 0},
        {"return_untyped": True},
        {"return_subclass": True},
    ],
)
def test_durable_qualification_receipt_must_exactly_echo_verified_record(
    overrides,
) -> None:
    contract = _contract(cutover=True)
    target_resolver = FakeTargetResolver()

    with pytest.raises(LedgerSafetyError):
        _qualified_production(
            contract,
            qualification_evidence=FakeQualificationEvidence(**overrides),
            target_resolver=target_resolver,
        )

    assert target_resolver.calls == 0


def test_production_target_is_resolved_fresh_after_long_pilot_qualification() -> None:
    contract = _contract(cutover=True)
    plan = _plan(contract)
    later = NOW + timedelta(minutes=6)
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(
        plan.operations[1],
        effect_confirmed_at=later - timedelta(seconds=30),
    )
    signals = tuple(
        replace(result, observed_at=later - timedelta(seconds=10))
        for result in _pilot_signal_results(
            contract.pilot_qualification_required_signals
        )
    )
    target_resolver = FakeTargetResolver(captured_at=later - timedelta(seconds=2))

    final = _qualified_production(
        contract,
        plan=plan,
        ledger=ledger,
        qualification_resolver=FakePilotQualificationResolver(
            signal_results=signals,
            verified_at=later - timedelta(seconds=5),
        ),
        qualification_evidence=FakeQualificationEvidence(
            recorded_at=later - timedelta(seconds=1)
        ),
        target_resolver=target_resolver,
        clock=FakeClock(later),
    )

    assert plan.operations[1].evidence_valid_until < later
    assert final.target_snapshot_hash != plan.operations[1].target_snapshot_hash
    assert final.evidence_valid_until > later
    assert target_resolver.calls == 1


def test_fresh_production_target_source_is_independent_from_qualification() -> None:
    contract = _contract(cutover=True)

    with pytest.raises(EvidenceRejected, match="target source is not independent"):
        _qualified_production(
            contract,
            target_resolver=FakeTargetResolver(
                source_identity="pilot-qualification-verifier"
            ),
        )


def test_fresh_production_target_uses_the_planned_resolver_identity() -> None:
    contract = _contract(cutover=True)

    with pytest.raises(EvidenceRejected, match="differs from the planned resolver"):
        _qualified_production(
            contract,
            target_resolver=FakeTargetResolver(
                source_identity="replacement-target-verifier"
            ),
        )


def test_oldest_check_result_bounds_final_production_execution_deadline() -> None:
    contract = _contract(cutover=True)
    check_results = list(_check_results(contract.required_checks))
    check_results[0] = replace(
        check_results[0],
        completed_at=NOW - timedelta(seconds=3599),
    )
    plan = _plan(
        contract,
        check_suite_resolver=FakeCheckSuiteResolver(results=tuple(check_results)),
    )
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(plan.operations[1])
    operation = _qualified_production(contract, plan=plan, ledger=ledger)
    provider = FakeProvider()

    assert operation.evidence_valid_until == NOW + timedelta(seconds=1)
    with pytest.raises(EvidenceRejected, match="expired"):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
            clock=FakeClock(NOW + timedelta(seconds=2)),
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_oldest_pilot_signal_bounds_production_execution_deadline() -> None:
    contract = _contract(cutover=True)
    plan = _plan(contract)
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(
        plan.operations[1],
        effect_confirmed_at=NOW - timedelta(seconds=400),
    )
    signal_results = list(
        _pilot_signal_results(contract.pilot_qualification_required_signals)
    )
    signal_results[0] = replace(
        signal_results[0],
        observed_at=NOW - timedelta(seconds=299),
    )
    operation = _qualified_production(
        contract,
        plan=plan,
        ledger=ledger,
        qualification_resolver=FakePilotQualificationResolver(
            signal_results=tuple(signal_results)
        ),
    )
    provider = FakeProvider()

    assert operation.evidence_valid_until == NOW + timedelta(seconds=1)
    with pytest.raises(EvidenceRejected, match="expired"):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
            clock=FakeClock(NOW + timedelta(seconds=2)),
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_correspondence_digest_has_no_artifact_or_target_input_slot() -> None:
    request_types = (
        ArtifactResolutionRequest,
        CheckSuiteVerificationRequest,
        QualificationRecordRequest,
        SourceCandidateVerificationRequest,
        TargetSnapshotRequest,
    )
    assert all(
        "correspondence_digest" not in {item.name for item in fields(request_type)}
        for request_type in request_types
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_digest", DIGEST_A),
        ("contract_edition", "different-edition"),
        ("required_checks", ("different-check",)),
        ("artifact_digest", DIGEST_B),
        ("embedded_layer_digest", DIGEST_C),
        ("artifact_resolver_identity", "other-artifact-verifier"),
        ("gate_evidence_hash", DIGEST_C),
        ("target_profile_digest", DIGEST_A),
        ("target_source_identity", "other-target-verifier"),
        ("expected_current_digest", DIGEST_A),
        ("target_config_digest", DIGEST_A),
        ("migration_history_digest", DIGEST_A),
        ("current_schema", 4),
        ("migration_class", MigrationClass.DATA_REWRITE),
        ("schema_max", 7),
        ("evidence_valid_until", "2026-09-03T13:00:00Z"),
        ("required_pilot_operation_fingerprint", DIGEST_A),
    ],
)
def test_operation_fingerprint_binds_contract_release_target_and_migration(
    field, value
) -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    payload = json.loads(canonical_json_bytes(operation.fingerprint_payload()))
    paths = {
        "contract_digest": ("contract", "digest"),
        "contract_edition": ("contract", "edition"),
        "required_checks": ("contract", "required_checks"),
        "artifact_digest": ("artifact", "digest"),
        "embedded_layer_digest": ("artifact", "embedded_layer_digest"),
        "artifact_resolver_identity": ("artifact", "resolver_identity"),
        "gate_evidence_hash": (None, "gate_evidence_hash"),
        "target_profile_digest": ("target", "profile_digest"),
        "target_source_identity": ("target", "source_identity"),
        "expected_current_digest": ("target", "expected_current_digest"),
        "target_config_digest": ("target", "target_config_digest"),
        "migration_history_digest": ("target", "migration_history_digest"),
        "current_schema": ("target", "current_schema"),
        "migration_class": ("migration", "class"),
        "schema_max": ("migration", "schema_max"),
        "evidence_valid_until": (None, "evidence_valid_until"),
        "required_pilot_operation_fingerprint": (
            None,
            "required_pilot_operation_fingerprint",
        ),
    }
    section, key = paths[field]
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, MigrationClass):
        value = value.value
    if section is None:
        payload[key] = value
    else:
        payload[section][key] = value

    assert canonical_sha256(payload) != operation.operation_fingerprint


def test_cutover_false_blocks_every_execution_before_authority_ledger_or_provider() -> (
    None
):
    contract = _contract(cutover=False)
    operation = _plan(contract).operations[0]
    verifier = FakeAuthorityVerifier()
    ledger = FakeLedger()
    provider = FakeProvider()

    with pytest.raises(CutoverBlocked):
        _execute(
            contract=contract,
            operation=operation,
            verifier=verifier,
            ledger=ledger,
            provider=provider,
        )

    assert verifier.calls == 0
    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_execute_rejects_operation_from_another_contract_before_provider() -> None:
    live = _contract(cutover=True)
    foreign = _contract(cutover=False)
    operation = _plan(live).operations[0]
    provider = FakeProvider()

    with pytest.raises(ContractError, match="digest"):
        _execute(contract=foreign, operation=operation, provider=provider)

    assert provider.calls == 0


@pytest.mark.parametrize(
    "receipt_overrides",
    [
        None,
        {"artifact_digest": DIGEST_B},
        {"embedded_manifest_hash": DIGEST_B},
        {"contract_digest": DIGEST_B},
        {"contract_edition": "old-edition"},
        {"required_checks_digest": DIGEST_B},
        {"operation_fingerprint": DIGEST_B},
        {"state": OperationState.OUTCOME_UNKNOWN},
        {"state_version": 1},
        {"durable": False},
        {"action": "promote"},
        {"target": "pilot"},
        {"state": "applied"},
        {"durable": 1},
    ],
)
def test_production_requires_exact_durable_pilot_applied_receipt(
    receipt_overrides,
) -> None:
    contract = _contract(cutover=True)
    operation = _qualified_production(contract)
    ledger = FakeLedger()
    provider = FakeProvider()
    if receipt_overrides is not None:
        ledger.pilot_receipt = _pilot_receipt(operation, **receipt_overrides)

    with pytest.raises(LedgerSafetyError, match="pilot"):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_exact_pilot_receipt_allows_production_to_reuse_same_artifact() -> None:
    contract = _contract(cutover=True)
    operation = _qualified_production(contract)
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(operation)
    provider = FakeProvider()

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
    )

    assert result.state is ExecutionState.APPLIED
    assert provider.calls == 1


def test_execute_operation_requires_negative_matrix_receipt_when_cutover_enabled() -> (
    None
):
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    ledger.force_missing_negative_matrix = True
    provider = FakeProvider()

    with pytest.raises(LedgerSafetyError, match="negative-matrix"):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
        )

    assert ledger.negative_matrix_calls == 1
    assert ledger.claim_calls == 0
    assert provider.calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_ref": DIGEST_B},
        {"repository": "other/repository"},
        {"contract_digest": DIGEST_B},
        {"contract_edition": "other-edition"},
        {"pilot_source_ref": "other-pilot-ref"},
        {"production_source_ref": "other-production-ref"},
    ],
)
def test_negative_matrix_receipt_mismatch_is_rejected(overrides) -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    values = {
        "evidence_ref": contract.negative_matrix_evidence_ref,
        "repository": contract.repository,
        "contract_digest": contract.contract_digest,
        "contract_edition": contract.contract_edition,
        "pilot_source_ref": contract.pilot_source_ref,
        "production_source_ref": contract.production_source_ref,
    }
    values.update(overrides)
    ledger = FakeLedger()
    ledger.negative_matrix_override = NegativeMatrixReceipt(
        evidence_ref=values["evidence_ref"],
        repository=values["repository"],
        contract_digest=values["contract_digest"],
        contract_edition=values["contract_edition"],
        pilot_source_ref=values["pilot_source_ref"],
        production_source_ref=values["production_source_ref"],
        verified_at=NOW,
        approver_identity="approver-negative-matrix",
        evidence_hash=DIGEST_F,
    )
    provider = FakeProvider()

    with pytest.raises(LedgerSafetyError, match="negative-matrix receipt mismatch"):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_stale_negative_matrix_receipt_is_rejected() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    ledger.negative_matrix_verified_at = NOW - timedelta(
        seconds=contract.negative_matrix_max_age_seconds + 1
    )
    provider = FakeProvider()

    with pytest.raises(EvidenceRejected, match="negative-matrix receipt is stale"):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_fresh_negative_matrix_receipt_allows_execution() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    provider = FakeProvider()

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
    )

    assert result.state is ExecutionState.APPLIED
    assert ledger.negative_matrix_calls == 1


def test_contract_requires_negative_matrix_evidence_ref_when_cutover_enabled() -> None:
    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    payload["cutover"]["enabled"] = True
    payload["cutover"]["negative_matrix_evidence_ref"] = None

    with pytest.raises(ContractError, match="negative_matrix_evidence_ref"):
        validate_release_control_contract(payload)


def test_contract_rejects_malformed_negative_matrix_evidence_ref() -> None:
    payload = loads_strict_json(CONTRACT_PATH.read_bytes())
    payload["cutover"]["enabled"] = True
    payload["cutover"]["negative_matrix_evidence_ref"] = "not-a-digest"

    with pytest.raises(ContractError):
        validate_release_control_contract(payload)


def test_pilot_receipt_release_identity_must_be_exact() -> None:
    contract = _contract(cutover=True)
    operation = _qualified_production(contract)
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(operation, release_id="different-release")
    provider = FakeProvider()

    with pytest.raises(LedgerSafetyError, match="release_id"):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"purpose": "other-purpose"},
        {"audience": "other-audience"},
        {"repository": "other/repository"},
        {"target": "pilot"},
        {"contract_digest": DIGEST_A},
        {"contract_edition": "old-edition"},
        {"required_checks_digest": DIGEST_A},
        {"runner_identity": "other-runner"},
        {"approver_identity": RUNNER},
    ],
)
def test_authority_scope_is_domain_separated_and_exact(overrides) -> None:
    contract = _contract(cutover=True)
    operation = _qualified_production(contract)
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(operation)
    provider = FakeProvider()

    with pytest.raises(AuthorityRejected):
        _execute(
            contract=contract,
            operation=operation,
            authority=_authority(operation, **overrides),
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_forged_or_expired_authority_has_zero_mutation_calls() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    for authority, clock in (
        (_authority(operation, signature="forged"), FakeClock()),
        (
            _authority(
                operation,
                issued_at=(NOW - timedelta(minutes=31))
                .isoformat()
                .replace("+00:00", "Z"),
                expires_at=(NOW - timedelta(minutes=1))
                .isoformat()
                .replace("+00:00", "Z"),
            ),
            FakeClock(),
        ),
    ):
        ledger = FakeLedger()
        provider = FakeProvider()
        with pytest.raises(AuthorityRejected):
            _execute(
                contract=contract,
                operation=operation,
                authority=authority,
                clock=clock,
                ledger=ledger,
                provider=provider,
            )
        assert ledger.claim_calls == 0
        assert provider.calls == 0


def test_unavailable_trusted_clock_has_zero_authority_ledger_or_provider_calls() -> (
    None
):
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    verifier = FakeAuthorityVerifier()
    ledger = FakeLedger()
    provider = FakeProvider()

    with pytest.raises(EvidenceRejected, match="clock"):
        _execute(
            contract=contract,
            operation=operation,
            clock=FailingClock(),
            verifier=verifier,
            ledger=ledger,
            provider=provider,
        )

    assert verifier.calls == 0
    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_authority_verifier_receives_canonical_domain_separated_bytes() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    verifier = FakeAuthorityVerifier()

    _execute(contract=contract, operation=operation, verifier=verifier)

    signed = json.loads(verifier.payloads[0])
    assert signed["purpose"] == AUTHORITY_PURPOSE
    assert signed["audience"] == AUTHORITY_AUDIENCE
    assert signed["repository"] == contract.repository
    assert signed["contract_digest"] == contract.contract_digest
    assert signed["target"] == "pilot"


@pytest.mark.parametrize(
    "overrides",
    [
        {"key_id": "other-key"},
        {"authority_id": "10000000-0000-4000-8000-000000000002"},
        {"signer_identity": "other-signer"},
        {"runner_identity": "other-runner"},
        {"signed_payload_digest": DIGEST_B},
        {"trust_root_identity": "foreign-authority-root"},
        {"signature_valid": False},
        {"independent_signer": False},
        {"return_untyped": True},
    ],
)
def test_authority_verification_receipt_must_be_independent_and_exact(
    overrides,
) -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    provider = FakeProvider()

    with pytest.raises(AuthorityRejected):
        _execute(
            contract=contract,
            operation=operation,
            verifier=FakeAuthorityVerifier(**overrides),
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_authority_parser_rejects_extra_fields_and_noncanonical_uuid() -> None:
    operation = _plan(_contract(cutover=True)).operations[0]
    payload = _authority(operation).signed_payload()
    payload.update({"signature": "valid", "unexpected": True})
    with pytest.raises(SchemaError, match="extra"):
        AuthorityEvidence.from_mapping(payload)

    payload.pop("unexpected")
    payload["authority_id"] = "NOT-A-UUID"
    with pytest.raises(SchemaError, match="UUID"):
        AuthorityEvidence.from_mapping(payload)


def test_ledger_claim_boundary_contains_only_bounded_opaque_values() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    authority = _authority(operation)
    ledger = FakeLedger()

    _execute(
        contract=contract,
        operation=operation,
        authority=authority,
        ledger=ledger,
    )

    assert {field.name for field in fields(LedgerClaimRequest)} == {
        "external_id",
        "authorization_id",
        "operation_fingerprint",
        "target_key",
        "artifact_digest",
        "operation_kind",
        "contract_digest",
        "evidence_hash",
    }
    assert ledger.last_claim_request is not None
    assert len(ledger.last_claim_request.external_id) == 68
    assert ledger.last_claim_request.evidence_hash != authority.evidence_hash
    assert not hasattr(ledger.last_claim_request, "signature")
    assert not hasattr(ledger.last_claim_request, "image_reference")


def test_provider_idempotency_is_derived_only_from_operation_fingerprint() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    provider = FakeProvider()

    _execute(contract=contract, operation=operation, provider=provider)

    assert provider.keys == [provider_idempotency_key(operation.operation_fingerprint)]
    assert provider.keys[0] != CLAIM_ID
    assert provider.requests[0].external_id == operation_external_id(
        operation.operation_fingerprint
    )
    assert provider.requests[0].claim_id == CLAIM_ID
    assert provider.requests[0].fencing_token == 10


def test_provider_request_binds_atomic_target_snapshot_and_trusted_time_window() -> (
    None
):
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    provider = FakeProvider()
    ledger = FakeLedger()

    result = _execute(
        contract=contract,
        operation=operation,
        provider=provider,
        ledger=ledger,
        clock=SequenceClock(NOW, NOW, NOW, NOW),
    )

    request = provider.requests[0]
    assert result.state is ExecutionState.APPLIED
    assert request.not_before == NOW
    assert request.not_before < request.not_after
    assert request.expected_current_artifact_digest == (
        operation.expected_current_digest
    )
    assert request.expected_target_config_digest == operation.target_config_digest
    assert request.expected_current_schema == operation.current_schema
    assert request.expected_migration_history_digest == (
        operation.migration_history_digest
    )
    assert request.target_snapshot_hash == operation.target_snapshot_hash
    assert ledger.intent_calls == 1
    assert ledger.last_intent_request.provider_intent_hash == request.intent_hash
    assert ledger.last_intent_request.not_before == request.not_before
    assert ledger.last_intent_request.not_after == request.not_after
    assert ledger.last_outcome_request is not None
    assert ledger.last_outcome_request.evidence_hash == provider.results[0].receipt_hash


def test_provider_is_never_called_without_an_exact_durable_intent() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    ledger.force_intent_conflict = True
    provider = FakeProvider()

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
    )

    assert result.state is ExecutionState.OUTCOME_UNKNOWN
    assert result.provider_called is False
    assert ledger.intent_calls == 1
    assert provider.calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_intent_hash": DIGEST_B},
        {"provider_not_before": NOW - timedelta(seconds=1)},
        {"provider_not_after": NOW + timedelta(seconds=1)},
        {"state": OperationState.OUTCOME_UNKNOWN},
        {"state_version": 1},
        {"state_version": 3},
        {"fencing_token": 11},
    ],
)
def test_provider_intent_cas_must_return_the_exact_next_snapshot(overrides) -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = IntentTamperingLedger(**overrides)
    provider = FakeProvider()

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
    )

    assert result.state is ExecutionState.OUTCOME_UNKNOWN
    assert result.provider_called is False
    assert provider.calls == 0


def test_slow_provider_intent_cas_rechecks_deadline_before_mutation() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    provider = FakeProvider()

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
        clock=SequenceClock(NOW, NOW, NOW, operation.evidence_valid_until),
    )

    assert result.state is ExecutionState.OUTCOME_UNKNOWN
    assert result.provider_called is False
    assert ledger.intent_calls == 1
    assert provider.calls == 0


def test_release_operation_cannot_be_constructed_without_verified_plan_seal() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    values = {
        item.name: getattr(operation, item.name)
        for item in fields(ReleaseOperation)
        if not item.name.startswith("_")
    }

    with pytest.raises(EvidenceRejected, match="verified planning"):
        ReleaseOperation(**values)
    with pytest.raises(EvidenceRejected, match="verified planning"):
        replace(operation, artifact_digest=DIGEST_B)


@pytest.mark.parametrize(
    "overrides",
    [
        {"disposition": "owner"},
        {"external_id": "rc1-" + "b" * 64},
        {"authorization_id": "10000000-0000-4000-8000-000000000002"},
        {"operation_fingerprint": DIGEST_B},
        {"target_key": TargetName.PRODUCTION},
        {"target_key": "pilot"},
        {"artifact_digest": DIGEST_B},
        {"operation_kind": ReleaseAction.ROLLBACK},
        {"operation_kind": "promote"},
        {"contract_digest": DIGEST_B},
        {"evidence_hash": DIGEST_B},
        {"state": OperationState.OUTCOME_UNKNOWN},
        {"state_version": 0},
        {"state_version": 2},
        {"fencing_token": 0},
        {"fencing_token": 11},
        {"durable": False},
        {"authority_use_burned": False},
        {"claim_id": "not-a-uuid"},
        {"claim_id": "20000000-0000-4000-8000-000000000002"},
    ],
)
def test_claim_must_echo_durable_burned_fenced_receipt_before_provider(
    overrides,
) -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = ClaimTamperingLedger(**overrides)
    provider = FakeProvider()

    with pytest.raises((LedgerSafetyError, SchemaError)):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 1
    assert provider.calls == 0


def test_twenty_duplicate_claims_have_one_owner_and_one_provider_call() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    authority = _authority(operation)
    ledger = FakeLedger()
    provider = FakeProvider()
    verifier = FakeAuthorityVerifier()

    def attempt():
        return _execute(
            contract=contract,
            operation=operation,
            authority=authority,
            ledger=ledger,
            provider=provider,
            verifier=verifier,
        ).state

    with ThreadPoolExecutor(max_workers=20) as pool:
        states = list(pool.map(lambda _: attempt(), range(20)))

    assert set(states) <= {ExecutionState.APPLIED, ExecutionState.OUTCOME_UNKNOWN}
    assert ExecutionState.APPLIED in states
    retry = _execute(
        contract=contract,
        operation=operation,
        authority=authority,
        ledger=ledger,
        provider=provider,
        verifier=verifier,
    )
    assert retry.state is ExecutionState.APPLIED
    assert retry.provider_called is False
    assert ledger.owner_count == 1
    assert provider.calls == 1


def test_retry_returns_durable_applied_after_outcome_cas_reply_is_lost() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = LostOutcomeReplyLedger()
    provider = FakeProvider()

    first = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
    )
    second = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
    )

    assert first.state is ExecutionState.OUTCOME_UNKNOWN
    assert second.state is ExecutionState.APPLIED
    assert second.provider_called is False
    assert provider.calls == 1
    assert (
        ledger.states[operation.operation_fingerprint].state is OperationState.APPLIED
    )


def test_retry_returns_durable_applied_after_transient_evidence_expires() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = LostOutcomeReplyLedger()
    provider = FakeProvider()
    verifier = FakeAuthorityVerifier()

    first = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
        verifier=verifier,
    )
    expired_clock = FakeClock(operation.evidence_valid_until + timedelta(seconds=1))
    second = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
        verifier=verifier,
        clock=expired_clock,
    )

    assert first.state is ExecutionState.OUTCOME_UNKNOWN
    assert second.state is ExecutionState.APPLIED
    assert second.provider_called is False
    assert expired_clock.calls == 0
    assert verifier.calls == 1
    assert ledger.claim_calls == 1
    assert provider.calls == 1


def test_existing_claimed_lock_blocks_a_second_owner_and_provider_call() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation, state=OperationState.CLAIMED_LOCKED, version=1)
    provider = FakeProvider()
    verifier = FakeAuthorityVerifier()
    clock = FakeClock()

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
        verifier=verifier,
        clock=clock,
    )

    assert result.state is ExecutionState.OUTCOME_UNKNOWN
    assert result.provider_called is False
    assert ledger.claim_calls == 0
    assert provider.calls == 0
    assert verifier.calls == 0
    assert clock.calls == 0


def test_raced_duplicate_preserves_winning_authority_evidence() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = RacingDuplicateEvidenceLedger(operation)
    provider = FakeProvider()

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
    )

    assert result.state is ExecutionState.APPLIED
    assert result.provider_called is False
    assert ledger.last_claim_request is not None
    assert ledger.claims[operation.operation_fingerprint].authorization_id != (
        ledger.last_claim_request.authorization_id
    )
    assert ledger.claims[operation.operation_fingerprint].evidence_hash == DIGEST_A
    assert ledger.last_claim_request.evidence_hash != DIGEST_A
    assert provider.calls == 0


@pytest.mark.parametrize("state_version", [1, 2])
def test_forged_duplicate_applied_never_returns_false_success(
    state_version: int,
) -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = ClaimTamperingLedger(
        disposition=ClaimDisposition.DUPLICATE,
        state=OperationState.APPLIED,
        state_version=state_version,
    )
    provider = FakeProvider()

    with pytest.raises((LedgerSafetyError, SchemaError)):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
        )

    assert provider.calls == 0


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), RuntimeError("crash")])
def test_timeout_or_caught_crash_is_unknown_and_never_retried(failure) -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    authority = _authority(operation)
    ledger = FakeLedger()
    provider = FakeProvider(failure=failure)

    first = _execute(
        contract=contract,
        operation=operation,
        authority=authority,
        ledger=ledger,
        provider=provider,
    )
    second = _execute(
        contract=contract,
        operation=operation,
        authority=authority,
        ledger=ledger,
        provider=provider,
    )

    assert first.state is ExecutionState.OUTCOME_UNKNOWN
    assert second.state is ExecutionState.OUTCOME_UNKNOWN
    assert provider.calls == 1
    assert (
        ledger.states[operation.operation_fingerprint].state
        is OperationState.OUTCOME_UNKNOWN
    )


def test_failed_applied_cas_returns_unknown_not_false_success() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    ledger.force_outcome_conflict = True

    result = _execute(contract=contract, operation=operation, ledger=ledger)

    assert result.state is ExecutionState.OUTCOME_UNKNOWN


@pytest.mark.parametrize(
    "overrides",
    [
        {"external_id": "rc1-" + "b" * 64},
        {"operation_fingerprint": DIGEST_B},
        {"target": TargetName.PRODUCTION},
        {"artifact_digest": DIGEST_B},
        {"contract_digest": DIGEST_B},
        {"state": OperationState.OUTCOME_UNKNOWN},
        {"state_version": 4},
        {"fencing_token": 11},
    ],
)
def test_applied_cas_must_return_the_exact_next_snapshot(overrides) -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = OutcomeTamperingLedger(**overrides)

    result = _execute(contract=contract, operation=operation, ledger=ledger)

    assert result.state is ExecutionState.OUTCOME_UNKNOWN


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation_fingerprint": DIGEST_B},
        {"external_id": "rc1-" + "b" * 64},
        {"claim_id": "20000000-0000-4000-8000-000000000002"},
        {"idempotency_key": DIGEST_B},
        {"fencing_token": 11},
        {"not_before": NOW - timedelta(seconds=1)},
        {"not_after": NOW + timedelta(seconds=1)},
        {"applied_at": NOW - timedelta(seconds=1)},
        {"applied_at": NOW + timedelta(hours=1)},
        {"observed_previous_artifact_digest": DIGEST_C},
        {"observed_target_config_digest": DIGEST_C},
        {"observed_current_schema": 4},
        {"observed_migration_history_digest": DIGEST_C},
        {"artifact_digest": DIGEST_B},
        {"image_reference": (f"registry.example.invalid/other/app@{DIGEST_A}")},
        {"target_profile_digest": DIGEST_B},
        {"target_snapshot_hash": DIGEST_B},
        {"provider_reference_hash": "invalid"},
        {"evidence_hash": "invalid"},
        {"return_untyped": True},
        {"return_subclass": True},
    ],
)
def test_mismatched_provider_response_is_unknown_and_never_retried(overrides) -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    provider = FakeProvider(result_overrides=overrides)

    first = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
    )
    second = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
    )

    assert first.state is ExecutionState.OUTCOME_UNKNOWN
    assert second.state is ExecutionState.OUTCOME_UNKNOWN
    assert provider.calls == 1


def test_expired_verified_plan_evidence_blocks_provider() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    later = NOW + timedelta(minutes=6)
    authority = _authority(
        operation,
        issued_at=(later - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        expires_at=(later + timedelta(minutes=29)).isoformat().replace("+00:00", "Z"),
    )
    ledger = FakeLedger()
    provider = FakeProvider()

    with pytest.raises(EvidenceRejected, match="expired"):
        _execute(
            contract=contract,
            operation=operation,
            authority=authority,
            clock=FakeClock(later),
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


def test_plan_evidence_expiring_after_claim_is_unknown_without_provider_call() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    provider = FakeProvider()
    clock = SequenceClock(NOW, NOW, NOW + timedelta(minutes=5))

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
        clock=clock,
    )

    assert result.state is ExecutionState.OUTCOME_UNKNOWN
    assert result.provider_called is False
    assert clock.calls == 3
    assert ledger.outcome_calls == 1
    assert ledger.states[operation.operation_fingerprint].state is (
        OperationState.OUTCOME_UNKNOWN
    )
    assert provider.calls == 0


def test_authority_expiring_after_claim_is_unknown_without_provider_call() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    authority = _authority(
        operation,
        expires_at=(NOW + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    )
    ledger = FakeLedger()
    provider = FakeProvider()

    result = _execute(
        contract=contract,
        operation=operation,
        authority=authority,
        ledger=ledger,
        provider=provider,
        clock=SequenceClock(NOW, NOW, NOW + timedelta(seconds=2)),
    )

    assert result.state is ExecutionState.OUTCOME_UNKNOWN
    assert result.provider_called is False
    assert ledger.states[operation.operation_fingerprint].state is (
        OperationState.OUTCOME_UNKNOWN
    )
    assert provider.calls == 0


def test_trusted_clock_failure_after_claim_is_unknown_without_provider_call() -> None:
    contract = _contract(cutover=True)
    operation = _plan(contract).operations[0]
    ledger = FakeLedger()
    provider = FakeProvider()

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
        provider=provider,
        clock=SequenceClock(NOW, NOW, RuntimeError("trusted time lost")),
    )

    assert result.state is ExecutionState.OUTCOME_UNKNOWN
    assert result.provider_called is False
    assert ledger.states[operation.operation_fingerprint].state is (
        OperationState.OUTCOME_UNKNOWN
    )
    assert provider.calls == 0


def test_single_zero_snapshot_never_finalizes_not_applied() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation)
    envelope = _envelope(operation, 1)

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver([envelope]),
        clock=FakeClock(),
    )

    assert result.resolved is False
    assert result.state is OperationState.OUTCOME_UNKNOWN
    assert ledger.reconcile_calls == 0


def test_reconciliation_rechecks_freshness_after_slow_observer() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation)
    observer = FakeObserver(
        [_envelope(operation, 1, observations=[_observation(operation)])]
    )

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=observer,
        clock=SequenceClock(NOW, NOW + timedelta(minutes=6)),
    )

    assert result.resolved is False
    assert result.state is OperationState.OUTCOME_UNKNOWN
    assert ledger.reconcile_calls == 0


def test_zero_observations_do_not_finalize_while_mutation_window_is_open() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation)
    envelopes = [
        _envelope(operation, 1, captured_at=NOW - timedelta(seconds=50)),
        _envelope(operation, 2, captured_at=NOW - timedelta(seconds=10)),
    ]

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver(envelopes),
        clock=FakeClock(),
    )

    assert NOW < operation.evidence_valid_until
    assert result.resolved is False
    assert result.state is OperationState.OUTCOME_UNKNOWN
    assert ledger.reconcile_calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"target": "pilot"},
        {"state": "outcome_unknown"},
    ],
)
def test_reconciliation_rejects_untyped_ledger_state(overrides) -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation)
    ledger.states[operation.operation_fingerprint] = replace(
        ledger.states[operation.operation_fingerprint],
        **overrides,
    )
    observer = FakeObserver([_envelope(operation, 1)])

    with pytest.raises(LedgerSafetyError, match="untyped"):
        reconcile_uncertain_operation(
            operation=operation,
            ledger=ledger,
            observer=observer,
            clock=FakeClock(),
        )

    assert observer.calls == 0


def test_two_fresh_complete_settled_zero_snapshots_finalize_by_cas() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation, version=7, fencing=41)
    after_mutation_horizon = operation.evidence_valid_until + timedelta(minutes=1)
    envelopes = [
        _envelope(
            operation,
            1,
            captured_at=after_mutation_horizon - timedelta(seconds=50),
            settled_through=after_mutation_horizon - timedelta(seconds=50),
        ),
        _envelope(
            operation,
            2,
            captured_at=after_mutation_horizon - timedelta(seconds=10),
            settled_through=after_mutation_horizon - timedelta(seconds=10),
        ),
    ]

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver(envelopes),
        clock=FakeClock(after_mutation_horizon),
    )

    assert result.resolved is True
    assert result.state is OperationState.OBSERVED_NOT_APPLIED
    assert ledger.last_reconcile_request.expected_version == 7
    assert ledger.last_reconcile_request.fencing_token == 41


def test_claimed_locked_crash_is_resolved_only_by_auditor_observations() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation, state=OperationState.CLAIMED_LOCKED)
    after_mutation_horizon = operation.evidence_valid_until + timedelta(minutes=1)
    envelopes = [
        _envelope(
            operation,
            1,
            captured_at=after_mutation_horizon - timedelta(seconds=50),
            settled_through=after_mutation_horizon - timedelta(seconds=50),
        ),
        _envelope(
            operation,
            2,
            captured_at=after_mutation_horizon - timedelta(seconds=10),
            settled_through=after_mutation_horizon - timedelta(seconds=10),
        ),
    ]

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver(envelopes),
        clock=FakeClock(after_mutation_horizon),
    )

    assert result.state is OperationState.OBSERVED_NOT_APPLIED
    assert result.resolved is True
    assert ledger.last_reconcile_request.expected_state is OperationState.CLAIMED_LOCKED


@pytest.mark.parametrize(
    "envelopes_factory",
    [
        lambda operation: [_envelope(operation, 1, complete=False)],
        lambda operation: [
            _envelope(operation, 1, captured_at=NOW - timedelta(seconds=301))
        ],
        lambda operation: [
            _envelope(
                operation,
                1,
                settled_through=NOW - timedelta(seconds=70),
            )
        ],
        lambda operation: [
            _envelope(operation, 1),
            _envelope(operation, 1, captured_at=NOW - timedelta(seconds=5)),
        ],
        lambda operation: [
            _envelope(operation, 1),
            _envelope(
                operation,
                2,
                captured_at=NOW - timedelta(seconds=5),
                evidence_hash=_sha("1"),
            ),
        ],
    ],
)
def test_incomplete_stale_unsettled_or_replayed_snapshots_do_not_cas(
    envelopes_factory,
) -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation)

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver(envelopes_factory(operation)),
        clock=FakeClock(),
    )

    assert result.resolved is False
    assert ledger.reconcile_calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation_fingerprint": DIGEST_B},
        {"target": TargetName.PRODUCTION},
        {"target_profile_digest": DIGEST_B},
    ],
)
def test_observation_envelope_scope_mismatch_never_reconciles(overrides) -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation)

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver([_envelope(operation, 1, **overrides)]),
        clock=FakeClock(),
    )

    assert result.resolved is False
    assert ledger.reconcile_calls == 0


@pytest.mark.parametrize(
    ("observations", "expected", "count"),
    [
        (
            lambda operation: [_observation(operation)],
            OperationState.OBSERVED_APPLIED,
            1,
        ),
        (
            lambda operation: [
                _observation(operation, reference=DIGEST_A),
                _observation(operation, reference=DIGEST_B),
            ],
            OperationState.MANUAL_REVIEW,
            2,
        ),
        (
            lambda operation: [
                _observation(
                    operation,
                    artifact_digest=DIGEST_B,
                    image_reference=f"registry.example.invalid/team/app@{DIGEST_B}",
                )
            ],
            OperationState.MANUAL_REVIEW,
            0,
        ),
    ],
)
def test_reconciliation_exact_one_many_or_contradiction(
    observations, expected, count
) -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation)
    envelope = _envelope(operation, 1, observations=observations(operation))

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver([envelope]),
        clock=FakeClock(),
    )

    assert result.resolved is True
    assert result.state is expected
    assert result.exact_match_count == count


def test_apply_and_reconciliation_share_one_provider_effect_schema() -> None:
    assert tuple(item.name for item in fields(ProviderObservation)) == tuple(
        item.name for item in fields(ProviderApplyResult)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_fingerprint", DIGEST_B),
        ("external_id", "other-external-id"),
        ("claim_id", "20000000-0000-4000-8000-000000000002"),
        ("idempotency_key", DIGEST_B),
        ("fencing_token", 20),
        ("not_before", NOW - timedelta(seconds=121)),
        ("not_after", NOW + timedelta(days=1)),
        ("applied_at", NOW - timedelta(seconds=100)),
        ("observed_previous_artifact_digest", DIGEST_A),
        ("observed_target_config_digest", DIGEST_A),
        ("observed_current_schema", 4),
        ("observed_migration_history_digest", DIGEST_A),
        ("artifact_digest", DIGEST_B),
        (
            "image_reference",
            f"registry.example.invalid/other/app@{DIGEST_A}",
        ),
        ("target_profile_digest", DIGEST_A),
        ("target_snapshot_hash", DIGEST_A),
    ],
)
def test_partial_or_drifted_provider_effect_never_reconciles_as_applied(
    field,
    value,
) -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation)
    overrides = {field: value}
    if field == "artifact_digest":
        overrides["image_reference"] = f"registry.example.invalid/team/app@{DIGEST_B}"
    observation = _observation(operation, **overrides)

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver([_envelope(operation, 1, observations=[observation])]),
        clock=FakeClock(),
    )

    assert result.state is not OperationState.OBSERVED_APPLIED
    assert ledger.states[operation.operation_fingerprint].state is not (
        OperationState.OBSERVED_APPLIED
    )


def test_reconciliation_cannot_widen_the_durable_provider_window() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    durable_deadline = NOW + timedelta(seconds=10)
    ledger.seed_uncertain(operation, intent_not_after=durable_deadline)
    widened = _observation(
        operation,
        applied_at=NOW + timedelta(seconds=20),
        not_after=operation.evidence_valid_until,
    )

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver(
            [
                _envelope(
                    operation,
                    1,
                    captured_at=NOW + timedelta(seconds=30),
                    observations=[widened],
                )
            ]
        ),
        clock=FakeClock(NOW + timedelta(seconds=40)),
    )

    assert result.state is OperationState.MANUAL_REVIEW
    assert result.exact_match_count == 0


def test_observed_effect_without_durable_provider_intent_requires_review() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation, with_provider_intent=False)

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver(
            [_envelope(operation, 1, observations=[_observation(operation)])]
        ),
        clock=FakeClock(),
    )

    assert result.state is OperationState.MANUAL_REVIEW
    assert result.exact_match_count == 0


def test_reconciliation_cas_conflict_leaves_original_state() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = FakeLedger()
    ledger.seed_uncertain(operation)
    ledger.force_reconcile_conflict = True

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver(
            [_envelope(operation, 1, observations=[_observation(operation)])]
        ),
        clock=FakeClock(),
    )

    assert result.resolved is False
    assert result.state is OperationState.OUTCOME_UNKNOWN
    assert (
        ledger.states[operation.operation_fingerprint].state
        is OperationState.OUTCOME_UNKNOWN
    )


def test_retry_returns_terminal_reconciliation_after_cas_reply_is_lost() -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = LostReconciliationReplyLedger()
    ledger.seed_uncertain(operation)
    observer = FakeObserver(
        [_envelope(operation, 1, observations=[_observation(operation)])]
    )

    first = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=observer,
        clock=FakeClock(),
    )
    second = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=observer,
        clock=FakeClock(),
    )

    assert first.resolved is False
    assert first.state is OperationState.OUTCOME_UNKNOWN
    assert second.resolved is True
    assert second.state is OperationState.OBSERVED_APPLIED
    assert second.exact_match_count == 1
    assert observer.calls == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"external_id": "rc1-" + "b" * 64},
        {"operation_fingerprint": DIGEST_B},
        {"target": TargetName.PRODUCTION},
        {"artifact_digest": DIGEST_B},
        {"contract_digest": DIGEST_B},
        {"state": OperationState.OUTCOME_UNKNOWN},
        {"state_version": 5},
        {"fencing_token": 20},
    ],
)
def test_reconciliation_cas_requires_exact_next_versioned_snapshot(overrides) -> None:
    operation = _plan(_contract(cutover=False)).operations[0]
    ledger = ReconcileTamperingLedger(**overrides)
    ledger.seed_uncertain(operation)

    result = reconcile_uncertain_operation(
        operation=operation,
        ledger=ledger,
        observer=FakeObserver(
            [_envelope(operation, 1, observations=[_observation(operation)])]
        ),
        clock=FakeClock(),
    )

    assert result.resolved is False
    assert result.state is OperationState.OUTCOME_UNKNOWN


def _rollback(
    contract,
    *,
    artifact=None,
    historical_contract=None,
    check_suite=None,
    target=None,
    rollback=None,
    compatibility=None,
    manifest=None,
):
    return plan_rollback(
        contract=contract,
        rollback_release_id="rollback-2026-09-03-01",
        previous_manifest=manifest or _manifest(contract),
        artifact_resolver=artifact or FakeArtifactResolver(),
        historical_contract_resolver=(
            historical_contract or FakeHistoricalBuildContractResolver()
        ),
        check_suite_resolver=check_suite or FakeCheckSuiteResolver(),
        target_resolver=target or FakeTargetResolver(),
        rollback_resolver=rollback or FakeRollbackResolver(),
        compatibility_resolver=(compatibility or FakeRollbackCompatibilityResolver()),
        clock=FakeClock(),
    )


def test_oldest_check_result_bounds_pilot_and_rollback_execution_deadlines() -> None:
    contract = _contract(cutover=True)
    check_results = list(_check_results(contract.required_checks))
    check_results[0] = replace(
        check_results[0],
        completed_at=NOW - timedelta(seconds=3599),
    )
    resolver = FakeCheckSuiteResolver(results=tuple(check_results))
    pilot_operation = _plan(contract, check_suite_resolver=resolver).operations[0]
    rollback_operation = _rollback(
        contract,
        check_suite=FakeCheckSuiteResolver(results=tuple(check_results)),
    )

    for operation in (pilot_operation, rollback_operation):
        ledger = FakeLedger()
        if operation.target is TargetName.PRODUCTION:
            ledger.pilot_receipt = _pilot_receipt(operation)
        provider = FakeProvider()

        assert operation.evidence_valid_until == NOW + timedelta(seconds=1)
        with pytest.raises(EvidenceRejected, match="expired"):
            _execute(
                contract=contract,
                operation=operation,
                ledger=ledger,
                provider=provider,
                clock=FakeClock(NOW + timedelta(seconds=2)),
            )

        assert ledger.claim_calls == 0
        assert provider.calls == 0


def test_rollback_reuses_verified_retained_previous_good_digest() -> None:
    contract = _contract(cutover=False)
    operation = _rollback(contract)

    assert operation.action is ReleaseAction.ROLLBACK
    assert operation.artifact_digest == DIGEST_A
    assert operation.current_schema == 5
    assert operation.migration_history_digest == DIGEST_E
    assert operation.required_pilot_operation_fingerprint == DIGEST_E
    assert operation.qualified_pilot_applied_receipt_hash is None
    assert operation.rollback_historical_contract_digest == contract.contract_digest
    assert operation.rollback_historical_contract_edition == "historical-contract-v1"
    assert operation.rollback_historical_receipt_hash is not None
    assert operation.rollback_compatibility_receipt_hash is not None


def test_rollback_accepts_sealed_historical_build_under_evolved_contract() -> None:
    contract = _contract(cutover=False)
    historical_manifest = _manifest(
        contract,
        build_contract_digest=DIGEST_B,
        required_checks=["historical-check"],
    )

    operation = _rollback(contract, manifest=historical_manifest)

    assert operation.contract_digest == contract.contract_digest
    assert operation.required_checks == contract.required_checks
    assert operation.rollback_historical_contract_digest == DIGEST_B
    assert operation.embedded_manifest_hash == historical_manifest.manifest_hash


def test_evolved_rollback_queries_exact_historical_pilot_receipt_scope() -> None:
    contract = _contract(cutover=True)
    historical_manifest = _manifest(
        contract,
        build_contract_digest=DIGEST_B,
        required_checks=["historical-check"],
    )
    operation = _rollback(contract, manifest=historical_manifest)
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(operation)

    result = _execute(
        contract=contract,
        operation=operation,
        ledger=ledger,
    )

    assert result.state is ExecutionState.APPLIED
    assert ledger.last_pilot_query.contract_digest == DIGEST_B
    assert ledger.last_pilot_query.contract_edition == "historical-contract-v1"
    assert ledger.last_pilot_query.required_checks_digest == (
        operation.rollback_historical_required_checks_digest
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_digest": DIGEST_A},
        {"historical_build_contract_digest": DIGEST_A},
        {"previous_manifest_hash": DIGEST_A},
        {"artifact_digest": DIGEST_B},
        {"historical_required_checks": ("other-check",)},
        {"archive_complete": False},
        {"immutable": False},
        {"verifier_identity": "check-suite-verifier"},
        {"sealed_at": NOW + timedelta(seconds=1)},
        {"return_untyped": True},
        {"return_subclass": True},
    ],
)
def test_historical_build_contract_receipt_is_exact_typed_and_sealed(
    overrides,
) -> None:
    with pytest.raises(RollbackRejected):
        _rollback(
            _contract(cutover=False),
            historical_contract=FakeHistoricalBuildContractResolver(**overrides),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_digest": DIGEST_A},
        {"current_contract_digest": DIGEST_A},
        {"historical_contract_digest": DIGEST_A},
        {"historical_contract_receipt_hash": DIGEST_A},
        {"artifact_digest": DIGEST_B},
        {"target_snapshot_hash": DIGEST_A},
        {"target_config_digest": DIGEST_A},
        {"current_schema": 4},
        {"migration_history_digest": DIGEST_A},
        {"rollback_capability_receipt_hash": DIGEST_A},
        {"compatible": False},
        {"complete": False},
        {"independent_verifier": False},
        {"verifier_identity": "rollback-verifier"},
        {"verifier_identity": "artifact-verifier"},
        {"verifier_identity": "target-state-verifier"},
        {"verified_at": NOW - timedelta(seconds=1801)},
        {"return_untyped": True},
        {"return_subclass": True},
    ],
)
def test_rollback_compatibility_is_exact_fresh_and_independent(overrides) -> None:
    with pytest.raises(RollbackRejected):
        _rollback(
            _contract(cutover=False),
            compatibility=FakeRollbackCompatibilityResolver(**overrides),
        )


def test_rollback_capability_receipt_is_bound_into_operation_fingerprint() -> None:
    contract = _contract(cutover=False)
    first = _rollback(contract)
    second = _rollback(
        contract,
        rollback=FakeRollbackResolver(evidence_hash=DIGEST_A),
    )

    assert first.gate_evidence_hash != second.gate_evidence_hash
    assert first.operation_fingerprint != second.operation_fingerprint


def test_rollback_binds_verified_pilot_fingerprint_and_requires_exact_receipt() -> None:
    contract = _contract(cutover=True)
    operation = _rollback(
        contract,
        rollback=FakeRollbackResolver(pilot_operation_fingerprint=DIGEST_B),
    )
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(
        operation,
        operation_fingerprint=DIGEST_C,
    )
    provider = FakeProvider()

    with pytest.raises(LedgerSafetyError, match="operation_fingerprint"):
        _execute(
            contract=contract,
            operation=operation,
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"request_digest": DIGEST_A}, "request_digest"),
        ({"previous_good_digest": DIGEST_B}, "previous_good_digest"),
        ({"previous_manifest_hash": DIGEST_B}, "previous_manifest_hash"),
        ({"target": TargetName.PILOT}, "target"),
        ({"target_snapshot_hash": DIGEST_B}, "target_snapshot_hash"),
        ({"expected_current_digest": DIGEST_A}, "expected_current_digest"),
        ({"current_schema": 4}, "current_schema"),
        ({"migration_history_digest": DIGEST_A}, "migration_history_digest"),
        ({"can_rollback": False}, "canRollback"),
        ({"artifact_retained": False}, "retained"),
        ({"artifact_origin": "rebuilt"}, "rebuild"),
        ({"runtime_attestation_valid": False}, "attestation"),
        ({"source_identity": "artifact-verifier"}, "independent"),
        ({"source_identity": "target-state-verifier"}, "independent"),
        ({"can_rollback_checked_at": NOW - timedelta(seconds=3601)}, "stale"),
        ({"retention_checked_at": NOW - timedelta(seconds=3601)}, "stale"),
        ({"runtime_reattested_at": NOW - timedelta(seconds=1801)}, "stale"),
        ({"return_untyped": True}, "untyped"),
    ],
)
def test_rollback_capability_equality_and_freshness_gates_fail_closed(
    overrides, message
) -> None:
    with pytest.raises(RollbackRejected, match=message):
        _rollback(
            _contract(cutover=False),
            rollback=FakeRollbackResolver(**overrides),
        )


def test_rollback_rejects_rebuilt_artifact_at_resolver_boundary() -> None:
    with pytest.raises(EvidenceRejected, match="provenance"):
        _rollback(
            _contract(cutover=False),
            artifact=FakeArtifactResolver(provenance="build_once"),
        )


def test_rollback_rejects_incompatible_current_schema_or_migration_history() -> None:
    contract = _contract(cutover=False)
    for overrides in (
        {"current_schema": 7},
        {"schema_compatible": False},
        {"migration_allowed": False},
    ):
        with pytest.raises(EvidenceRejected):
            _rollback(contract, target=FakeTargetResolver(**overrides))


@pytest.mark.parametrize(
    "field",
    [
        "can_rollback_checked_at",
        "retention_checked_at",
        "runtime_reattested_at",
    ],
)
def test_rollback_rejects_future_dated_re_attestation(field) -> None:
    with pytest.raises(RollbackRejected, match="future"):
        _rollback(
            _contract(cutover=False),
            rollback=FakeRollbackResolver(**{field: NOW + timedelta(seconds=1)}),
        )


def test_rollback_re_attestation_deadline_is_enforced_again_at_execution() -> None:
    contract = _contract(cutover=True)
    operation = _rollback(
        contract,
        target=FakeTargetResolver(captured_at=NOW),
        rollback=FakeRollbackResolver(
            can_rollback_checked_at=NOW - timedelta(seconds=3599),
            retention_checked_at=NOW - timedelta(seconds=3599),
        ),
    )
    later = NOW + timedelta(seconds=2)
    authority = _authority(
        operation,
        issued_at=(NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        expires_at=(NOW + timedelta(minutes=29)).isoformat().replace("+00:00", "Z"),
    )
    ledger = FakeLedger()
    ledger.pilot_receipt = _pilot_receipt(operation)
    provider = FakeProvider()

    assert operation.evidence_valid_until == NOW + timedelta(seconds=1)
    with pytest.raises(EvidenceRejected, match="expired"):
        _execute(
            contract=contract,
            operation=operation,
            authority=authority,
            clock=FakeClock(later),
            ledger=ledger,
            provider=provider,
        )

    assert ledger.claim_calls == 0
    assert provider.calls == 0
