"""Verified, transport-neutral release-control core for WP-562.

The module owns policy and deterministic state transitions, not credentials or
provider transports. Every fact that can authorize a mutation enters through a
narrow verified port. The current repository contract keeps cutover off, so the
code can build and inspect plans but cannot call even an injected provider.

Live adapters must preserve three ordering invariants:

* an independent, domain-separated authority is verified with trusted time;
* the ledger durably claims and burns that authority before the provider call;
* any uncertain claim is reconciled by fresh, complete observations and an
  atomic compare-and-swap, never by retrying the mutation.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import re
import stat
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RELEASE_MANIFEST_PATH = _REPO_ROOT / "release_manifest.py"


def _load_exact_release_manifest() -> object:
    try:
        metadata = _RELEASE_MANIFEST_PATH.lstat()
    except OSError as exc:
        raise ImportError("trusted release-manifest module is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or _RELEASE_MANIFEST_PATH.is_symlink():
        raise ImportError("trusted release-manifest module is not a regular file")

    existing = sys.modules.get("release_manifest")
    if existing is not None:
        origin = getattr(existing, "__file__", None)
        if origin is None or Path(origin).resolve() != _RELEASE_MANIFEST_PATH.resolve():
            raise ImportError("ambiguous release-manifest module is forbidden")
        return existing

    spec = importlib.util.spec_from_file_location(
        "release_manifest", _RELEASE_MANIFEST_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError("trusted release-manifest module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_manifest"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("release_manifest", None)
        raise
    return module


_release_manifest = _load_exact_release_manifest()
EmbeddedManifest = _release_manifest.EmbeddedManifest
MigrationClass = _release_manifest.MigrationClass
ReleaseManifestError = _release_manifest.ReleaseManifestError
canonical_json_bytes = _release_manifest.canonical_json_bytes
canonical_sha256 = _release_manifest.canonical_sha256
loads_strict_json = _release_manifest.loads_strict_json

LOGGER = logging.getLogger(__name__)

OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
AUTHORITY_PURPOSE = "iwe.release-control.authority.v1"
AUTHORITY_AUDIENCE = "iwe.release-control.runner"
PROVIDER_IDEMPOTENCY_DOMAIN = "iwe.release-control.provider-idempotency.v1"
PILOT_QUALIFICATION_REQUIRED_SIGNALS = (
    "canary",
    "data-invariants",
    "readiness",
    "runtime-attestation",
)

MAX_IDENTIFIER_LENGTH = 128
MAX_REFERENCE_LENGTH = 512
MAX_TARGET_SNAPSHOT_AGE_SECONDS = 300
MAX_ARTIFACT_RECEIPT_AGE_SECONDS = 900
RECONCILIATION_MAX_SNAPSHOT_AGE_SECONDS = 300
RECONCILIATION_MIN_SETTLING_SECONDS = 60
RECONCILIATION_MIN_ZERO_INTERVAL_SECONDS = 30
RECONCILIATION_MIN_ZERO_SNAPSHOTS = 2

AUTHORITY_REQUIRED_SIGNED_FIELDS = (
    "approver_identity",
    "audience",
    "authority_id",
    "contract_digest",
    "contract_edition",
    "expires_at",
    "issued_at",
    "key_id",
    "max_uses",
    "nonce",
    "operation_fingerprint",
    "purpose",
    "release_id",
    "repository",
    "required_checks_digest",
    "runner_identity",
    "schema_version",
    "target",
)
LEDGER_CLAIM_FIELD_ALLOWLIST = (
    "artifact_digest",
    "authorization_id",
    "contract_digest",
    "evidence_hash",
    "external_id",
    "operation_fingerprint",
    "operation_kind",
    "target_key",
)

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_CHECK_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_REPOSITORY_PART_RE = re.compile(r"[a-z0-9](?:[a-z0-9_.-]{0,62}[a-z0-9])?\Z")
_OCI_COMPONENT_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")


class ReleaseControlError(ValueError):
    """Base class for release-control failures that must stop mutation."""


class SchemaError(ReleaseControlError):
    """A value does not match its exact bounded schema."""


class ContractError(ReleaseControlError):
    """The repository release-control contract is malformed or unsafe."""


class EvidenceRejected(ReleaseControlError):
    """A trusted resolver did not provide exact, fresh evidence."""


class AuthorityRejected(ReleaseControlError):
    """The independent mutation authority is invalid or out of scope."""


class CutoverBlocked(ReleaseControlError):
    """Mutation is disabled by the immutable repository contract."""


class LedgerSafetyError(ReleaseControlError):
    """A durable state transition did not prove its safety invariants."""


class RollbackRejected(ReleaseControlError):
    """Rollback evidence is incomplete, stale, or mismatched."""


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{path} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise SchemaError(f"{path} keys must be strings")
    return value


def _exact_keys(value: Any, expected: set[str], path: str) -> Mapping[str, Any]:
    obj = _mapping(value, path)
    actual = set(obj)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(f"missing={missing}")
        if extra:
            parts.append(f"extra={extra}")
        raise SchemaError(f"{path} has wrong keys ({', '.join(parts)})")
    return obj


def _literal(value: Any, expected: Any, path: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise SchemaError(f"{path} must equal {expected!r}")


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise SchemaError(f"{path} must be a boolean")
    return value


def _bounded_int(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SchemaError(f"{path} is outside its integer range")
    return value


def _bounded_string(
    value: Any,
    path: str,
    *,
    maximum: int = MAX_IDENTIFIER_LENGTH,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SchemaError(f"{path} must be non-empty trimmed text")
    if len(value) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise SchemaError(f"{path} exceeds its safe bounds")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise SchemaError(f"{path} has invalid characters")
    return value


def _safe_id(value: Any, path: str) -> str:
    return _bounded_string(value, path, pattern=_SAFE_ID_RE)


def _canonical_uuid(value: Any, path: str) -> str:
    text = _bounded_string(value, path, maximum=36)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise SchemaError(f"{path} must be a canonical UUID") from exc
    if str(parsed) != text:
        raise SchemaError(f"{path} must be a lowercase canonical UUID")
    return text


def _digest(value: Any, path: str) -> str:
    text = _bounded_string(value, path, maximum=71)
    if _SHA256_RE.fullmatch(text) is None:
        raise SchemaError(f"{path} must be a lowercase sha256 digest")
    return text


def _git_oid(value: Any, path: str) -> str:
    text = _bounded_string(value, path, maximum=64)
    if _GIT_OID_RE.fullmatch(text) is None:
        raise SchemaError(f"{path} must be a full lowercase Git object ID")
    return text


def _utc(value: Any, path: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise SchemaError(f"{path} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _parse_utc(value: Any, path: str) -> datetime:
    text = _bounded_string(value, path, maximum=35)
    if not text.endswith("Z"):
        raise SchemaError(f"{path} must use UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise SchemaError(f"{path} is not an ISO-8601 timestamp") from exc
    return _utc(parsed, path)


def _timestamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _age_seconds(observed_at: datetime, now: datetime, path: str) -> float:
    age = (_utc(now, "trusted clock") - _utc(observed_at, path)).total_seconds()
    if age < 0:
        raise EvidenceRejected(f"{path} is in the future")
    return age


def _hash(value: Any, path: str) -> str:
    try:
        return canonical_sha256(value)
    except ReleaseManifestError as exc:
        raise SchemaError(f"{path} is not canonical JSON") from exc


def _check_ids(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 32:
        raise SchemaError(f"{path} must contain 1..32 check IDs")
    result = tuple(
        _bounded_string(item, f"{path}[]", maximum=64, pattern=_CHECK_ID_RE)
        for item in value
    )
    if result != tuple(sorted(set(result))):
        raise SchemaError(f"{path} must be sorted and unique")
    return result


def _repository_slug(owner: Any, name: Any) -> str:
    owner_text = _bounded_string(
        owner,
        "contract.repository.owner",
        pattern=_REPOSITORY_PART_RE,
    )
    name_text = _bounded_string(
        name,
        "contract.repository.name",
        pattern=_REPOSITORY_PART_RE,
    )
    return f"{owner_text}/{name_text}"


def _git_ref(value: Any, path: str) -> str:
    ref = _bounded_string(value, path, maximum=255)
    forbidden = ("..", "@{", "//", " ", "~", "^", ":", "?", "*", "[", "\\")
    if (
        ref in ("@", ".")
        or ref.startswith(("/", "."))
        or ref.endswith(("/", "."))
        or any(token in ref for token in forbidden)
    ):
        raise SchemaError(f"{path} is not a safe Git ref")
    components = ref.split("/")
    if any(
        not component or component.startswith(".") or component.endswith((".", ".lock"))
        for component in components
    ):
        raise SchemaError(f"{path} is not a canonical Git ref")
    return ref


def _relative_path(value: Any, path: str) -> str:
    text = _bounded_string(value, path, maximum=256)
    candidate = Path(text)
    if (
        candidate.is_absolute()
        or text in (".", "..")
        or ".." in candidate.parts
        or "\\" in text
        or "//" in text
        or text.endswith("/")
        or candidate.as_posix() != text
    ):
        raise SchemaError(f"{path} must be a bounded repository-relative path")
    return text


def validate_immutable_image_reference(value: Any, expected_digest: str) -> str:
    """Validate a strict digest-only OCI reference, allowing a registry port."""

    reference = _bounded_string(
        value,
        "artifact.image_reference",
        maximum=MAX_REFERENCE_LENGTH,
    )
    if (
        reference.count("@") != 1
        or "://" in reference
        or any(marker in reference for marker in ("?", "#"))
    ):
        raise SchemaError(
            "artifact.image_reference must be a digest-only OCI reference"
        )
    name, digest = reference.rsplit("@", 1)
    if digest != _digest(expected_digest, "artifact.digest"):
        raise SchemaError("artifact.image_reference digest mismatch")
    components = name.split("/")
    if not components or any(not component for component in components):
        raise SchemaError("artifact.image_reference has an empty name component")
    registry = components[0]
    if ":" in registry:
        host, port = registry.rsplit(":", 1)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise SchemaError("artifact.image_reference has an invalid registry port")
        registry = host
    if _OCI_COMPONENT_RE.fullmatch(registry) is None:
        raise SchemaError("artifact.image_reference has an invalid registry")
    for component in components[1:]:
        if ":" in component:
            raise SchemaError("artifact.image_reference must not contain a mutable tag")
        if _OCI_COMPONENT_RE.fullmatch(component) is None:
            raise SchemaError(
                "artifact.image_reference has an invalid repository component"
            )
    if ":" in components[-1] and len(components) == 1:
        raise SchemaError("artifact.image_reference must not contain a mutable tag")
    return reference


def _oci_repository(value: Any, path: str) -> str:
    repository = _bounded_string(value, path, maximum=440)
    if "@" in repository or "://" in repository or len(repository.split("/")) < 2:
        raise SchemaError(f"{path} must name one canonical OCI registry/repository")
    probe_digest = "sha256:" + "0" * 64
    validate_immutable_image_reference(f"{repository}@{probe_digest}", probe_digest)
    return repository


_RELEASE_CONTRACT_SEAL = object()


@dataclass(frozen=True)
class ReleaseControlContract:
    schema_version: int
    contract_edition: str
    repository: str
    oci_repository: str
    pilot_source_ref: str
    production_source_ref: str
    authority_trust_root_identity: str
    authority_ttl_seconds: int
    reconciliation_settling_seconds: int
    zero_settlement_min_interval_seconds: int
    zero_settlement_observations: int
    check_suite_max_age_seconds: int
    source_candidate_max_age_seconds: int
    pilot_qualification_max_age_seconds: int
    pilot_qualification_required_signals: tuple[str, ...]
    rollback_compatibility_max_age_seconds: int
    rollback_retention_recheck_seconds: int
    rollback_reattestation_seconds: int
    required_checks: tuple[str, ...]
    protected_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    cutover_enabled: bool
    negative_matrix_max_age_seconds: int
    negative_matrix_evidence_ref: str | None
    contract_digest: str
    _validated_seal: InitVar[object | None] = None
    _validated: bool = dataclass_field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self, _validated_seal: object | None) -> None:
        if _validated_seal is not _RELEASE_CONTRACT_SEAL:
            raise ContractError("contract requires strict repository validation")
        object.__setattr__(self, "_validated", True)

    @property
    def required_checks_digest(self) -> str:
        return _hash(list(self.required_checks), "contract.required_checks")


_CONTRACT_KEYS = {
    "schema_version",
    "contract_edition",
    "repository",
    "source_refs",
    "artifact",
    "authority",
    "ledger",
    "promotion",
    "rollback",
    "required_checks",
    "protected_paths",
    "forbidden_paths",
    "cutover",
}


def _validate_contract_identity(
    root: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    _literal(root["schema_version"], 1, "contract.schema_version")
    edition = _safe_id(root["contract_edition"], "contract.contract_edition")
    repository = _exact_keys(
        root["repository"], {"owner", "name"}, "contract.repository"
    )
    repository_slug = _repository_slug(repository["owner"], repository["name"])
    refs = _exact_keys(
        root["source_refs"], {"pilot", "production"}, "contract.source_refs"
    )
    pilot_ref = _git_ref(refs["pilot"], "contract.source_refs.pilot")
    production_ref = _git_ref(refs["production"], "contract.source_refs.production")
    if pilot_ref == production_ref:
        raise SchemaError("contract source refs must be distinct")
    return edition, repository_slug, pilot_ref, production_ref


def _validate_contract_artifact(value: Any) -> str:
    artifact = _exact_keys(
        value,
        {
            "digest_kind",
            "oci_repository",
            "platform",
            "immutable_reference_required",
            "embedded_manifest_excludes",
        },
        "contract.artifact",
    )
    _literal(artifact["digest_kind"], "oci_manifest", "contract.artifact.digest_kind")
    oci_repository = _oci_repository(
        artifact["oci_repository"],
        "contract.artifact.oci_repository",
    )
    platform = _exact_keys(
        artifact["platform"], {"os", "architecture"}, "contract.artifact.platform"
    )
    _literal(platform["os"], "linux", "contract.artifact.platform.os")
    _literal(
        platform["architecture"],
        "amd64",
        "contract.artifact.platform.architecture",
    )
    _literal(
        artifact["immutable_reference_required"],
        True,
        "contract.artifact.immutable_reference_required",
    )
    if artifact["embedded_manifest_excludes"] != ["artifact_digest", "manifest_hash"]:
        raise SchemaError("contract artifact must exclude self/final digests")
    return oci_repository


def _validate_contract_authority(value: Any) -> tuple[int, str]:
    authority = _exact_keys(
        value,
        {
            "audience",
            "independent_approver_required",
            "max_uses",
            "purpose",
            "required_signed_fields",
            "trust_root_identity",
            "ttl_seconds",
        },
        "contract.authority",
    )
    _literal(authority["purpose"], AUTHORITY_PURPOSE, "contract.authority.purpose")
    _literal(authority["audience"], AUTHORITY_AUDIENCE, "contract.authority.audience")
    if authority["required_signed_fields"] != list(AUTHORITY_REQUIRED_SIGNED_FIELDS):
        raise SchemaError("contract authority signed-field allowlist mismatch")
    _literal(
        authority["independent_approver_required"],
        True,
        "contract.authority.independent_approver_required",
    )
    _literal(authority["max_uses"], 1, "contract.authority.max_uses")
    ttl_seconds = _bounded_int(
        authority["ttl_seconds"],
        "contract.authority.ttl_seconds",
        minimum=1,
        maximum=86_400,
    )
    trust_root = _safe_id(
        authority["trust_root_identity"],
        "contract.authority.trust_root_identity",
    )
    return ttl_seconds, trust_root


def _validate_contract_ledger(value: Any) -> None:
    ledger = _exact_keys(
        value,
        {
            "append_only_required",
            "claim_field_allowlist",
            "durable_claim_required",
            "external_receipt_required",
            "raw_evidence_forbidden",
            "target_version_cas_required",
        },
        "contract.ledger",
    )
    for key in (
        "durable_claim_required",
        "append_only_required",
        "external_receipt_required",
        "raw_evidence_forbidden",
        "target_version_cas_required",
    ):
        _literal(ledger[key], True, f"contract.ledger.{key}")
    if ledger["claim_field_allowlist"] != list(LEDGER_CLAIM_FIELD_ALLOWLIST):
        raise SchemaError("contract ledger claim-field allowlist mismatch")


def _validate_contract_promotion(value: Any) -> tuple[int, int, int]:
    promotion = _exact_keys(
        value,
        {
            "build_once",
            "check_suite_max_age_seconds",
            "check_suite_receipt_required",
            "independent_review_required",
            "pilot_qualification_independent_verifier_required",
            "pilot_qualification_max_age_seconds",
            "pilot_qualification_receipt_required",
            "pilot_qualification_required_signals",
            "provider_fencing_required",
            "provider_deadline_required",
            "provider_idempotency_required",
            "reconciliation",
            "reconciliation_settling_seconds",
            "same_digest_required",
            "source_candidate_max_age_seconds",
            "source_candidate_receipt_required",
            "zero_settlement_min_interval_seconds",
            "zero_settlement_observations",
        },
        "contract.promotion",
    )
    literal_fields = {
        "build_once": True,
        "check_suite_receipt_required": True,
        "independent_review_required": True,
        "pilot_qualification_independent_verifier_required": True,
        "pilot_qualification_receipt_required": True,
        "provider_fencing_required": True,
        "provider_deadline_required": True,
        "provider_idempotency_required": True,
        "reconciliation_settling_seconds": RECONCILIATION_MIN_SETTLING_SECONDS,
        "same_digest_required": True,
        "source_candidate_receipt_required": True,
        "zero_settlement_min_interval_seconds": RECONCILIATION_MIN_ZERO_INTERVAL_SECONDS,
        "zero_settlement_observations": RECONCILIATION_MIN_ZERO_SNAPSHOTS,
    }
    for key, expected in literal_fields.items():
        _literal(promotion[key], expected, f"contract.promotion.{key}")
    check_suite_max_age = _bounded_int(
        promotion["check_suite_max_age_seconds"],
        "contract.promotion.check_suite_max_age_seconds",
        minimum=1,
        maximum=86_400,
    )
    source_candidate_max_age = _bounded_int(
        promotion["source_candidate_max_age_seconds"],
        "contract.promotion.source_candidate_max_age_seconds",
        minimum=1,
        maximum=86_400,
    )
    pilot_qualification_max_age = _bounded_int(
        promotion["pilot_qualification_max_age_seconds"],
        "contract.promotion.pilot_qualification_max_age_seconds",
        minimum=1,
        maximum=3600,
    )
    if promotion["pilot_qualification_required_signals"] != list(
        PILOT_QUALIFICATION_REQUIRED_SIGNALS
    ):
        raise SchemaError("contract pilot qualification signal allowlist mismatch")
    reconciliation = _exact_keys(
        promotion["reconciliation"],
        {"zero", "one", "many"},
        "contract.promotion.reconciliation",
    )
    expected_outcomes = {
        "zero": "observed_not_applied",
        "one": "observed_applied",
        "many": "manual_review",
    }
    for cardinality, expected in expected_outcomes.items():
        _literal(
            reconciliation[cardinality],
            expected,
            f"contract.promotion.reconciliation.{cardinality}",
        )
    return (
        check_suite_max_age,
        source_candidate_max_age,
        pilot_qualification_max_age,
    )


def _validate_contract_rollback(value: Any) -> tuple[int, int, int]:
    rollback = _exact_keys(
        value,
        {
            "compatibility_max_age_seconds",
            "compatibility_receipt_required",
            "historical_build_contract_receipt_required",
            "independent_verifier_required",
            "same_previous_good_digest",
            "rebuild_forbidden",
            "require_can_rollback",
            "require_schema_compatibility",
            "retention_recheck_seconds",
            "reattestation_seconds",
        },
        "contract.rollback",
    )
    for key in (
        "compatibility_receipt_required",
        "historical_build_contract_receipt_required",
        "independent_verifier_required",
        "same_previous_good_digest",
        "rebuild_forbidden",
        "require_can_rollback",
        "require_schema_compatibility",
    ):
        _literal(rollback[key], True, f"contract.rollback.{key}")
    compatibility_max_age = _bounded_int(
        rollback["compatibility_max_age_seconds"],
        "contract.rollback.compatibility_max_age_seconds",
        minimum=1,
        maximum=86_400,
    )
    retention_recheck = _bounded_int(
        rollback["retention_recheck_seconds"],
        "contract.rollback.retention_recheck_seconds",
        minimum=1,
        maximum=86_400,
    )
    reattestation = _bounded_int(
        rollback["reattestation_seconds"],
        "contract.rollback.reattestation_seconds",
        minimum=1,
        maximum=86_400,
    )
    return compatibility_max_age, retention_recheck, reattestation


def _validate_contract_checks_and_paths(
    root: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    checks = _check_ids(root["required_checks"], "contract.required_checks")
    raw_paths = root["protected_paths"]
    if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= 256:
        raise SchemaError("contract.protected_paths must contain 1..256 paths")
    paths = tuple(
        _relative_path(item, "contract.protected_paths[]") for item in raw_paths
    )
    if paths != tuple(sorted(set(paths))):
        raise SchemaError("contract.protected_paths must be sorted and unique")
    if "scripts/release_control.py" not in paths:
        raise SchemaError("contract must protect scripts/release_control.py")
    raw_forbidden = root["forbidden_paths"]
    if not isinstance(raw_forbidden, list) or not 1 <= len(raw_forbidden) <= 64:
        raise SchemaError("contract.forbidden_paths must contain 1..64 paths")
    forbidden = tuple(
        _relative_path(item, "contract.forbidden_paths[]") for item in raw_forbidden
    )
    if forbidden != tuple(sorted(set(forbidden))):
        raise SchemaError("contract.forbidden_paths must be sorted and unique")
    overlap = sorted(set(paths).intersection(forbidden))
    if overlap:
        raise SchemaError("contract protected_paths and forbidden_paths overlap")
    return checks, paths, forbidden


def _validate_contract_cutover(value: Any) -> tuple[bool, int, str | None]:
    cutover = _exact_keys(
        value,
        {
            "enabled",
            "negative_matrix_required",
            "negative_matrix_max_age_seconds",
            "negative_matrix_evidence_ref",
        },
        "contract.cutover",
    )
    enabled = _boolean(cutover["enabled"], "contract.cutover.enabled")
    _literal(
        cutover["negative_matrix_required"],
        True,
        "contract.cutover.negative_matrix_required",
    )
    negative_matrix_max_age = _bounded_int(
        cutover["negative_matrix_max_age_seconds"],
        "contract.cutover.negative_matrix_max_age_seconds",
        minimum=1,
        maximum=86_400,
    )
    raw_evidence_ref = cutover["negative_matrix_evidence_ref"]
    if raw_evidence_ref is None:
        if enabled:
            raise SchemaError(
                "contract.cutover.negative_matrix_evidence_ref is required "
                "while cutover.enabled is true"
            )
        evidence_ref = None
    else:
        evidence_ref = _digest(
            raw_evidence_ref,
            "contract.cutover.negative_matrix_evidence_ref",
        )
    return enabled, negative_matrix_max_age, evidence_ref


def validate_release_control_contract(payload: Any) -> ReleaseControlContract:
    """Validate all contract fields; missing or unknown fields stop execution."""

    try:
        root = _exact_keys(payload, _CONTRACT_KEYS, "contract")
        edition, repository, pilot_ref, production_ref = _validate_contract_identity(
            root
        )
        oci_repository = _validate_contract_artifact(root["artifact"])
        authority_ttl, trust_root = _validate_contract_authority(root["authority"])
        _validate_contract_ledger(root["ledger"])
        (
            check_suite_max_age,
            source_candidate_max_age,
            pilot_qualification_max_age,
        ) = _validate_contract_promotion(root["promotion"])
        (
            rollback_compatibility_max_age,
            retention_recheck,
            reattestation,
        ) = _validate_contract_rollback(root["rollback"])
        checks, paths, forbidden_paths = _validate_contract_checks_and_paths(root)
        (
            cutover_enabled,
            negative_matrix_max_age,
            negative_matrix_evidence_ref,
        ) = _validate_contract_cutover(root["cutover"])
        contract_digest = _hash(root, "contract")
    except (SchemaError, ReleaseManifestError) as exc:
        raise ContractError(str(exc)) from exc

    return ReleaseControlContract(
        schema_version=1,
        contract_edition=edition,
        repository=repository,
        oci_repository=oci_repository,
        pilot_source_ref=pilot_ref,
        production_source_ref=production_ref,
        authority_trust_root_identity=trust_root,
        authority_ttl_seconds=authority_ttl,
        reconciliation_settling_seconds=RECONCILIATION_MIN_SETTLING_SECONDS,
        zero_settlement_min_interval_seconds=RECONCILIATION_MIN_ZERO_INTERVAL_SECONDS,
        zero_settlement_observations=RECONCILIATION_MIN_ZERO_SNAPSHOTS,
        check_suite_max_age_seconds=check_suite_max_age,
        source_candidate_max_age_seconds=source_candidate_max_age,
        pilot_qualification_max_age_seconds=pilot_qualification_max_age,
        pilot_qualification_required_signals=(PILOT_QUALIFICATION_REQUIRED_SIGNALS),
        rollback_compatibility_max_age_seconds=(rollback_compatibility_max_age),
        rollback_retention_recheck_seconds=retention_recheck,
        rollback_reattestation_seconds=reattestation,
        required_checks=checks,
        protected_paths=paths,
        forbidden_paths=forbidden_paths,
        cutover_enabled=cutover_enabled,
        negative_matrix_max_age_seconds=negative_matrix_max_age,
        negative_matrix_evidence_ref=negative_matrix_evidence_ref,
        contract_digest=contract_digest,
        _validated_seal=_RELEASE_CONTRACT_SEAL,
    )


def load_release_control_contract(path: str | Path) -> ReleaseControlContract:
    try:
        raw = Path(path).read_bytes()
        payload = loads_strict_json(raw)
    except OSError as exc:
        raise ContractError("cannot read release-control contract") from exc
    except ReleaseManifestError as exc:
        raise ContractError(str(exc)) from exc
    return validate_release_control_contract(payload)


class TrustedClockPort(Protocol):
    def now_utc(self) -> datetime:
        """Return authenticated wall-clock time, independent of request input."""


class _MonotonicTrustedClock:
    """Reject backward trusted-time movement across one complete flow."""

    def __init__(self, clock: TrustedClockPort) -> None:
        self._clock = clock
        self._last: datetime | None = None

    def now_utc(self) -> datetime:
        current = _utc(self._clock.now_utc(), "trusted clock")
        if self._last is not None and current < self._last:
            raise EvidenceRejected(
                "trusted clock moved backwards across release stages"
            )
        self._last = current
        return current


def _trusted_now(clock: TrustedClockPort) -> datetime:
    try:
        return _utc(clock.now_utc(), "trusted clock")
    except Exception as exc:
        if isinstance(exc, ReleaseControlError):
            raise EvidenceRejected(str(exc)) from exc
        LOGGER.warning("trusted clock failed closed (%s)", type(exc).__name__)
        raise EvidenceRejected("trusted clock unavailable") from exc


def _trusted_post_call_now(
    clock: TrustedClockPort,
    started_at: datetime,
    activity: str,
) -> datetime:
    """Close a trusted time window around an external evidence call."""

    finished_at = _trusted_now(clock)
    if finished_at < started_at:
        raise EvidenceRejected(f"trusted clock moved backwards during {activity}")
    return finished_at


def _canonical_manifest(manifest: EmbeddedManifest) -> EmbeddedManifest:
    if type(manifest) is not EmbeddedManifest:
        raise SchemaError("manifest must be a validated EmbeddedManifest")
    try:
        canonical = EmbeddedManifest.from_mapping(manifest.to_mapping())
    except (ReleaseManifestError, AttributeError, TypeError) as exc:
        raise SchemaError("embedded manifest failed canonical revalidation") from exc
    if canonical != manifest or canonical.schema_version != 1:
        raise SchemaError("embedded manifest is not canonical schema version 1")
    return canonical


def _validate_manifest(
    manifest: EmbeddedManifest,
    contract: ReleaseControlContract,
) -> EmbeddedManifest:
    if type(contract) is not ReleaseControlContract or not contract._validated:
        raise ContractError("contract was not produced by strict validation")
    canonical = _canonical_manifest(manifest)
    if canonical.build_contract_digest != contract.contract_digest:
        raise SchemaError("embedded manifest build contract digest does not match")
    if tuple(canonical.required_checks) != contract.required_checks:
        raise SchemaError("embedded manifest checks do not exactly match the contract")
    return canonical


class TargetName(str, Enum):
    PILOT = "pilot"
    PRODUCTION = "production"


class ReleaseAction(str, Enum):
    PROMOTE = "promote"
    ROLLBACK = "rollback"


class ArtifactPurpose(str, Enum):
    PROMOTION = "promotion"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class ArtifactResolutionRequest:
    repository: str
    contract_digest: str
    contract_edition: str
    oci_repository: str
    release_id: str
    embedded_manifest_hash: str
    purpose: ArtifactPurpose

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "oci_repository": self.oci_repository,
            "release_id": self.release_id,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "purpose": self.purpose.value,
        }

    @property
    def request_digest(self) -> str:
        return _hash(self.to_mapping(), "artifact resolution request")


@dataclass(frozen=True)
class ArtifactResolutionReceipt:
    schema_version: int
    receipt_id: str
    request_digest: str
    repository: str
    contract_digest: str
    contract_edition: str
    release_id: str
    artifact_digest: str
    digest_kind: str
    media_type: str
    platform_os: str
    platform_architecture: str
    image_reference: str
    embedded_layer_digest: str
    embedded_content_hash: str
    provenance: str
    resolver_identity: str
    resolved_at: datetime
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "artifact_receipt.schema_version")
        _canonical_uuid(self.receipt_id, "artifact_receipt.receipt_id")
        _digest(self.request_digest, "artifact_receipt.request_digest")
        _bounded_string(self.repository, "artifact_receipt.repository", maximum=129)
        _digest(self.contract_digest, "artifact_receipt.contract_digest")
        _safe_id(self.contract_edition, "artifact_receipt.contract_edition")
        _safe_id(self.release_id, "artifact_receipt.release_id")
        _digest(self.artifact_digest, "artifact_receipt.artifact_digest")
        _bounded_string(self.digest_kind, "artifact_receipt.digest_kind", maximum=32)
        _bounded_string(self.media_type, "artifact_receipt.media_type", maximum=128)
        _bounded_string(self.platform_os, "artifact_receipt.platform_os", maximum=16)
        _bounded_string(
            self.platform_architecture,
            "artifact_receipt.platform_architecture",
            maximum=16,
        )
        _bounded_string(
            self.image_reference,
            "artifact_receipt.image_reference",
            maximum=MAX_REFERENCE_LENGTH,
        )
        _digest(self.embedded_layer_digest, "artifact_receipt.embedded_layer_digest")
        _digest(self.embedded_content_hash, "artifact_receipt.embedded_content_hash")
        _bounded_string(self.provenance, "artifact_receipt.provenance", maximum=32)
        _safe_id(self.resolver_identity, "artifact_receipt.resolver_identity")
        _utc(self.resolved_at, "artifact_receipt.resolved_at")
        _digest(self.evidence_hash, "artifact_receipt.evidence_hash")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "request_digest": self.request_digest,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "release_id": self.release_id,
            "artifact_digest": self.artifact_digest,
            "digest_kind": self.digest_kind,
            "media_type": self.media_type,
            "platform": {
                "os": self.platform_os,
                "architecture": self.platform_architecture,
            },
            "image_reference": self.image_reference,
            "embedded_layer_digest": self.embedded_layer_digest,
            "embedded_content_hash": self.embedded_content_hash,
            "provenance": self.provenance,
            "resolver_identity": self.resolver_identity,
            "resolved_at": _timestamp(self.resolved_at),
            "evidence_hash": self.evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "artifact receipt")


class VerifiedArtifactResolverPort(Protocol):
    def resolve_verified(
        self,
        request: ArtifactResolutionRequest,
    ) -> ArtifactResolutionReceipt:
        """Resolve and verify registry plus embedded-layer/content evidence."""


_ARTIFACT_DESCRIPTOR_SEAL = object()


@dataclass(frozen=True, init=False)
class ArtifactDescriptor:
    image_reference: str
    digest: str
    embedded_manifest_hash: str
    embedded_layer_digest: str
    resolution_receipt_hash: str
    resolver_identity: str
    provenance: str
    resolved_at: datetime

    def __init__(self) -> None:
        raise EvidenceRejected("artifact descriptor requires a verified resolver")

    @classmethod
    def _from_verified_receipt(
        cls,
        receipt: ArtifactResolutionReceipt,
        seal: object,
    ) -> ArtifactDescriptor:
        if seal is not _ARTIFACT_DESCRIPTOR_SEAL:
            raise EvidenceRejected("artifact descriptor requires a verified resolver")
        descriptor = object.__new__(cls)
        object.__setattr__(descriptor, "image_reference", receipt.image_reference)
        object.__setattr__(descriptor, "digest", receipt.artifact_digest)
        object.__setattr__(
            descriptor, "embedded_manifest_hash", receipt.embedded_content_hash
        )
        object.__setattr__(
            descriptor, "embedded_layer_digest", receipt.embedded_layer_digest
        )
        object.__setattr__(descriptor, "resolution_receipt_hash", receipt.receipt_hash)
        object.__setattr__(descriptor, "resolver_identity", receipt.resolver_identity)
        object.__setattr__(descriptor, "provenance", receipt.provenance)
        object.__setattr__(descriptor, "resolved_at", receipt.resolved_at)
        return descriptor


def _resolve_artifact(
    *,
    contract: ReleaseControlContract,
    manifest: EmbeddedManifest,
    purpose: ArtifactPurpose,
    resolver: VerifiedArtifactResolverPort,
    clock: TrustedClockPort,
) -> ArtifactDescriptor:
    request = ArtifactResolutionRequest(
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        oci_repository=contract.oci_repository,
        release_id=manifest.release_id,
        embedded_manifest_hash=manifest.manifest_hash,
        purpose=purpose,
    )
    started_at = _trusted_now(clock)
    try:
        receipt = resolver.resolve_verified(request)
    except Exception as exc:
        LOGGER.warning("artifact resolver failed closed (%s)", type(exc).__name__)
        raise EvidenceRejected("verified artifact resolver failed") from exc
    now = _trusted_post_call_now(clock, started_at, "artifact resolution")
    if type(receipt) is not ArtifactResolutionReceipt:
        raise EvidenceRejected("artifact resolver returned an untyped assertion")
    expected = {
        "request_digest": request.request_digest,
        "repository": contract.repository,
        "contract_digest": contract.contract_digest,
        "contract_edition": contract.contract_edition,
        "release_id": manifest.release_id,
        "digest_kind": "oci_manifest",
        "media_type": OCI_MANIFEST_MEDIA_TYPE,
        "platform_os": "linux",
        "platform_architecture": "amd64",
        "embedded_content_hash": manifest.manifest_hash,
    }
    for field, value in expected.items():
        if getattr(receipt, field) != value:
            raise EvidenceRejected(f"artifact receipt mismatch: {field}")
    try:
        validate_immutable_image_reference(
            receipt.image_reference,
            receipt.artifact_digest,
        )
    except SchemaError as exc:
        raise EvidenceRejected(str(exc)) from exc
    expected_reference = f"{contract.oci_repository}@{receipt.artifact_digest}"
    if receipt.image_reference != expected_reference:
        raise EvidenceRejected(
            "artifact receipt is outside the contract OCI repository"
        )
    expected_provenance = (
        "build_once" if purpose is ArtifactPurpose.PROMOTION else "retained"
    )
    if receipt.provenance != expected_provenance:
        raise EvidenceRejected("artifact provenance does not match operation purpose")
    if _age_seconds(receipt.resolved_at, now, "artifact_receipt.resolved_at") > (
        MAX_ARTIFACT_RECEIPT_AGE_SECONDS
    ):
        raise EvidenceRejected("artifact resolution receipt is stale")
    return ArtifactDescriptor._from_verified_receipt(
        receipt,
        _ARTIFACT_DESCRIPTOR_SEAL,
    )


@dataclass(frozen=True)
class CheckSuiteVerificationRequest:
    repository: str
    contract_digest: str
    contract_edition: str
    release_id: str
    source_commit: str
    source_tree: str
    embedded_manifest_hash: str
    artifact_digest: str
    required_checks: tuple[str, ...]
    forbidden_paths: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "release_id": self.release_id,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "artifact_digest": self.artifact_digest,
            "required_checks": list(self.required_checks),
            "forbidden_paths": list(self.forbidden_paths),
        }

    @property
    def request_digest(self) -> str:
        return _hash(self.to_mapping(), "check-suite verification request")


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    conclusion: str
    completed_at: datetime
    suite_reference_hash: str
    provider_reference_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _bounded_string(
            self.check_id,
            "check_result.check_id",
            maximum=64,
            pattern=_CHECK_ID_RE,
        )
        _safe_id(self.conclusion, "check_result.conclusion")
        _utc(self.completed_at, "check_result.completed_at")
        _digest(
            self.suite_reference_hash,
            "check_result.suite_reference_hash",
        )
        _digest(
            self.provider_reference_hash,
            "check_result.provider_reference_hash",
        )
        _digest(self.evidence_hash, "check_result.evidence_hash")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "conclusion": self.conclusion,
            "completed_at": _timestamp(self.completed_at),
            "suite_reference_hash": self.suite_reference_hash,
            "provider_reference_hash": self.provider_reference_hash,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True)
class CheckSuiteReceipt:
    schema_version: int
    receipt_id: str
    request_digest: str
    repository: str
    contract_digest: str
    contract_edition: str
    release_id: str
    source_commit: str
    source_tree: str
    embedded_manifest_hash: str
    artifact_digest: str
    required_checks: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    complete: bool
    forbidden_paths_absent: bool
    suite_reference_hash: str
    results: tuple[CheckResult, ...]
    verified_at: datetime
    verifier_identity: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "check_suite.schema_version")
        _canonical_uuid(self.receipt_id, "check_suite.receipt_id")
        _digest(self.request_digest, "check_suite.request_digest")
        _bounded_string(self.repository, "check_suite.repository", maximum=129)
        _digest(self.contract_digest, "check_suite.contract_digest")
        _safe_id(self.contract_edition, "check_suite.contract_edition")
        _safe_id(self.release_id, "check_suite.release_id")
        _git_oid(self.source_commit, "check_suite.source_commit")
        _git_oid(self.source_tree, "check_suite.source_tree")
        _digest(
            self.embedded_manifest_hash,
            "check_suite.embedded_manifest_hash",
        )
        _digest(self.artifact_digest, "check_suite.artifact_digest")
        _check_ids(self.required_checks, "check_suite.required_checks")
        if self.forbidden_paths != tuple(sorted(set(self.forbidden_paths))):
            raise SchemaError("check_suite.forbidden_paths must be sorted and unique")
        for path in self.forbidden_paths:
            _relative_path(path, "check_suite.forbidden_paths[]")
        _boolean(self.complete, "check_suite.complete")
        _boolean(
            self.forbidden_paths_absent,
            "check_suite.forbidden_paths_absent",
        )
        _digest(
            self.suite_reference_hash,
            "check_suite.suite_reference_hash",
        )
        if not isinstance(self.results, tuple) or not 1 <= len(self.results) <= 32:
            raise SchemaError("check_suite.results must be a bounded tuple")
        if any(type(result) is not CheckResult for result in self.results):
            raise SchemaError("check_suite contains an untyped check result")
        _utc(self.verified_at, "check_suite.verified_at")
        _safe_id(self.verifier_identity, "check_suite.verifier_identity")
        _digest(self.evidence_hash, "check_suite.evidence_hash")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "request_digest": self.request_digest,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "release_id": self.release_id,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "artifact_digest": self.artifact_digest,
            "required_checks": list(self.required_checks),
            "forbidden_paths": list(self.forbidden_paths),
            "complete": self.complete,
            "forbidden_paths_absent": self.forbidden_paths_absent,
            "suite_reference_hash": self.suite_reference_hash,
            "results": [result.to_mapping() for result in self.results],
            "verified_at": _timestamp(self.verified_at),
            "verifier_identity": self.verifier_identity,
            "evidence_hash": self.evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "check-suite receipt")


class VerifiedCheckSuiteResolverPort(Protocol):
    def resolve_verified(
        self,
        request: CheckSuiteVerificationRequest,
    ) -> CheckSuiteReceipt:
        """Verify exact successful checks and forbidden-path absence."""


def _resolve_check_suite(
    *,
    contract: ReleaseControlContract,
    manifest: EmbeddedManifest,
    artifact: ArtifactDescriptor,
    resolver: VerifiedCheckSuiteResolverPort,
    clock: TrustedClockPort,
) -> CheckSuiteReceipt:
    request = CheckSuiteVerificationRequest(
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        release_id=manifest.release_id,
        source_commit=manifest.source_commit,
        source_tree=manifest.source_tree,
        embedded_manifest_hash=manifest.manifest_hash,
        artifact_digest=artifact.digest,
        required_checks=contract.required_checks,
        forbidden_paths=contract.forbidden_paths,
    )
    started_at = _trusted_now(clock)
    try:
        receipt = resolver.resolve_verified(request)
    except Exception as exc:
        LOGGER.warning("check-suite resolver failed closed (%s)", type(exc).__name__)
        raise EvidenceRejected("verified check-suite resolver failed") from exc
    now = _trusted_post_call_now(clock, started_at, "check-suite resolution")
    if type(receipt) is not CheckSuiteReceipt:
        raise EvidenceRejected("check-suite resolver returned an untyped assertion")
    expected = {
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
    }
    for field_name, expected_value in expected.items():
        if getattr(receipt, field_name) != expected_value:
            raise EvidenceRejected(f"check-suite receipt mismatch: {field_name}")
    check_ids = tuple(result.check_id for result in receipt.results)
    if check_ids != contract.required_checks:
        raise EvidenceRejected("check-suite results are not exact, sorted, and unique")
    if any(result.conclusion != "success" for result in receipt.results):
        raise EvidenceRejected("check-suite contains a non-success conclusion")
    if any(
        result.suite_reference_hash != receipt.suite_reference_hash
        for result in receipt.results
    ):
        raise EvidenceRejected("check-suite mixes results from different suites")
    if len({result.provider_reference_hash for result in receipt.results}) != len(
        receipt.results
    ):
        raise EvidenceRejected("check-suite reuses a provider check reference")
    if len({result.evidence_hash for result in receipt.results}) != len(
        receipt.results
    ):
        raise EvidenceRejected("check-suite reuses check evidence")
    verified_at = _utc(receipt.verified_at, "check_suite.verified_at")
    if _age_seconds(verified_at, now, "check_suite.verified_at") > (
        contract.check_suite_max_age_seconds
    ):
        raise EvidenceRejected("check-suite receipt is stale")
    if any(
        _utc(result.completed_at, "check_result.completed_at") > verified_at
        for result in receipt.results
    ):
        raise EvidenceRejected("check-suite result completed after verification")
    if any(
        _age_seconds(result.completed_at, now, "check_result.completed_at")
        > contract.check_suite_max_age_seconds
        for result in receipt.results
    ):
        raise EvidenceRejected("check-suite contains a stale result")
    return receipt


def _check_suite_valid_until(
    receipt: CheckSuiteReceipt,
    *,
    max_age_seconds: int,
) -> datetime:
    """Return the earliest expiry of the wrapper and every check result."""

    oldest_evidence = min(
        _utc(receipt.verified_at, "check_suite.verified_at"),
        *(
            _utc(result.completed_at, "check_result.completed_at")
            for result in receipt.results
        ),
    )
    return oldest_evidence + timedelta(seconds=max_age_seconds)


@dataclass(frozen=True)
class SourceCandidateVerificationRequest:
    repository: str
    contract_digest: str
    contract_edition: str
    release_id: str
    source_commit: str
    source_tree: str
    embedded_manifest_hash: str
    artifact_digest: str
    candidate_ref: str
    base_ref: str
    check_suite_receipt_hash: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "release_id": self.release_id,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "artifact_digest": self.artifact_digest,
            "candidate_ref": self.candidate_ref,
            "base_ref": self.base_ref,
            "check_suite_receipt_hash": self.check_suite_receipt_hash,
        }

    @property
    def request_digest(self) -> str:
        return _hash(self.to_mapping(), "source-candidate verification request")


@dataclass(frozen=True)
class SourceCandidateReceipt:
    schema_version: int
    receipt_id: str
    request_digest: str
    repository: str
    contract_digest: str
    contract_edition: str
    release_id: str
    source_commit: str
    source_tree: str
    embedded_manifest_hash: str
    artifact_digest: str
    candidate_ref: str
    base_ref: str
    check_suite_receipt_hash: str
    candidate_head_commit: str
    candidate_head_tree: str
    base_commit: str
    base_tree: str
    reviewed_source_commit: str
    reviewed_source_tree: str
    reviewed_base_commit: str
    reviewed_base_tree: str
    candidate_ref_reachable: bool
    independent_review: bool
    complete: bool
    reviewer_identity: str
    verifier_identity: str
    review_reference_hash: str
    verified_at: datetime
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "source_candidate.schema_version")
        _canonical_uuid(self.receipt_id, "source_candidate.receipt_id")
        _digest(self.request_digest, "source_candidate.request_digest")
        _bounded_string(self.repository, "source_candidate.repository", maximum=129)
        _digest(self.contract_digest, "source_candidate.contract_digest")
        _safe_id(self.contract_edition, "source_candidate.contract_edition")
        _safe_id(self.release_id, "source_candidate.release_id")
        for field_name in (
            "source_commit",
            "source_tree",
            "candidate_head_commit",
            "candidate_head_tree",
            "base_commit",
            "base_tree",
            "reviewed_source_commit",
            "reviewed_source_tree",
            "reviewed_base_commit",
            "reviewed_base_tree",
        ):
            _git_oid(getattr(self, field_name), f"source_candidate.{field_name}")
        for field_name in (
            "embedded_manifest_hash",
            "artifact_digest",
            "check_suite_receipt_hash",
            "review_reference_hash",
            "evidence_hash",
        ):
            _digest(getattr(self, field_name), f"source_candidate.{field_name}")
        _git_ref(self.candidate_ref, "source_candidate.candidate_ref")
        _git_ref(self.base_ref, "source_candidate.base_ref")
        _boolean(
            self.candidate_ref_reachable,
            "source_candidate.candidate_ref_reachable",
        )
        _boolean(self.independent_review, "source_candidate.independent_review")
        _boolean(self.complete, "source_candidate.complete")
        _safe_id(self.reviewer_identity, "source_candidate.reviewer_identity")
        _safe_id(self.verifier_identity, "source_candidate.verifier_identity")
        _utc(self.verified_at, "source_candidate.verified_at")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "request_digest": self.request_digest,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "release_id": self.release_id,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "artifact_digest": self.artifact_digest,
            "candidate_ref": self.candidate_ref,
            "base_ref": self.base_ref,
            "check_suite_receipt_hash": self.check_suite_receipt_hash,
            "candidate_head_commit": self.candidate_head_commit,
            "candidate_head_tree": self.candidate_head_tree,
            "base_commit": self.base_commit,
            "base_tree": self.base_tree,
            "reviewed_source_commit": self.reviewed_source_commit,
            "reviewed_source_tree": self.reviewed_source_tree,
            "reviewed_base_commit": self.reviewed_base_commit,
            "reviewed_base_tree": self.reviewed_base_tree,
            "candidate_ref_reachable": self.candidate_ref_reachable,
            "independent_review": self.independent_review,
            "complete": self.complete,
            "reviewer_identity": self.reviewer_identity,
            "verifier_identity": self.verifier_identity,
            "review_reference_hash": self.review_reference_hash,
            "verified_at": _timestamp(self.verified_at),
            "evidence_hash": self.evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "source-candidate receipt")


class VerifiedSourceCandidateResolverPort(Protocol):
    def resolve_verified(
        self,
        request: SourceCandidateVerificationRequest,
    ) -> SourceCandidateReceipt:
        """Verify candidate reachability and independent review for exact source."""


def _resolve_source_candidate(
    *,
    contract: ReleaseControlContract,
    manifest: EmbeddedManifest,
    artifact: ArtifactDescriptor,
    check_suite: CheckSuiteReceipt,
    resolver: VerifiedSourceCandidateResolverPort,
    clock: TrustedClockPort,
) -> SourceCandidateReceipt:
    request = SourceCandidateVerificationRequest(
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        release_id=manifest.release_id,
        source_commit=manifest.source_commit,
        source_tree=manifest.source_tree,
        embedded_manifest_hash=manifest.manifest_hash,
        artifact_digest=artifact.digest,
        candidate_ref=contract.pilot_source_ref,
        base_ref=contract.production_source_ref,
        check_suite_receipt_hash=check_suite.receipt_hash,
    )
    started_at = _trusted_now(clock)
    try:
        receipt = resolver.resolve_verified(request)
    except Exception as exc:
        LOGGER.warning(
            "source-candidate resolver failed closed (%s)",
            type(exc).__name__,
        )
        raise EvidenceRejected("verified source-candidate resolver failed") from exc
    now = _trusted_post_call_now(clock, started_at, "source-candidate resolution")
    if type(receipt) is not SourceCandidateReceipt:
        raise EvidenceRejected(
            "source-candidate resolver returned an untyped assertion"
        )
    expected = {
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
        "reviewed_source_commit": request.source_commit,
        "reviewed_source_tree": request.source_tree,
        "candidate_ref_reachable": True,
        "independent_review": True,
        "complete": True,
    }
    for field_name, expected_value in expected.items():
        if getattr(receipt, field_name) != expected_value:
            raise EvidenceRejected(f"source-candidate receipt mismatch: {field_name}")
    if (
        receipt.reviewed_base_commit != receipt.base_commit
        or receipt.reviewed_base_tree != receipt.base_tree
    ):
        raise EvidenceRejected("source-candidate reviewed base snapshot mismatch")
    if receipt.reviewer_identity == receipt.verifier_identity or (
        check_suite.verifier_identity
        in {receipt.reviewer_identity, receipt.verifier_identity}
    ):
        raise EvidenceRejected("source-candidate review is not independent")
    if _age_seconds(receipt.verified_at, now, "source_candidate.verified_at") > (
        contract.source_candidate_max_age_seconds
    ):
        raise EvidenceRejected("source-candidate receipt is stale")
    return receipt


@dataclass(frozen=True)
class TargetSnapshotRequest:
    repository: str
    contract_digest: str
    contract_edition: str
    operation_release_id: str
    candidate_release_id: str
    target: TargetName
    candidate_artifact_digest: str
    candidate_manifest_hash: str
    migration_class: MigrationClass
    schema_min: int
    schema_max: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "operation_release_id": self.operation_release_id,
            "candidate_release_id": self.candidate_release_id,
            "target": self.target.value,
            "candidate_artifact_digest": self.candidate_artifact_digest,
            "candidate_manifest_hash": self.candidate_manifest_hash,
            "migration_class": self.migration_class.value,
            "schema": {"min": self.schema_min, "max": self.schema_max},
        }

    @property
    def request_digest(self) -> str:
        return _hash(self.to_mapping(), "target snapshot request")


@dataclass(frozen=True)
class TargetSnapshotReceipt:
    schema_version: int
    snapshot_id: str
    request_digest: str
    repository: str
    contract_digest: str
    contract_edition: str
    operation_release_id: str
    candidate_release_id: str
    target: TargetName
    candidate_artifact_digest: str
    candidate_manifest_hash: str
    target_profile_digest: str
    current_artifact_digest: str
    current_manifest_hash: str
    target_config_digest: str
    current_schema: int
    migration_history_digest: str
    migration_class: MigrationClass
    schema_compatible: bool
    migration_allowed: bool
    complete: bool
    captured_at: datetime
    source_identity: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "target_snapshot.schema_version")
        _canonical_uuid(self.snapshot_id, "target_snapshot.snapshot_id")
        for field in (
            "request_digest",
            "contract_digest",
            "candidate_artifact_digest",
            "candidate_manifest_hash",
            "target_profile_digest",
            "current_artifact_digest",
            "current_manifest_hash",
            "target_config_digest",
            "migration_history_digest",
            "evidence_hash",
        ):
            _digest(getattr(self, field), f"target_snapshot.{field}")
        _bounded_string(self.repository, "target_snapshot.repository", maximum=129)
        _safe_id(self.contract_edition, "target_snapshot.contract_edition")
        _safe_id(self.operation_release_id, "target_snapshot.operation_release_id")
        _safe_id(self.candidate_release_id, "target_snapshot.candidate_release_id")
        if not isinstance(self.target, TargetName):
            raise SchemaError("target_snapshot.target must be a TargetName")
        if not isinstance(self.migration_class, MigrationClass):
            raise SchemaError(
                "target_snapshot.migration_class must be a MigrationClass"
            )
        _bounded_int(
            self.current_schema,
            "target_snapshot.current_schema",
            minimum=0,
            maximum=2_147_483_647,
        )
        _boolean(self.schema_compatible, "target_snapshot.schema_compatible")
        _boolean(self.migration_allowed, "target_snapshot.migration_allowed")
        _boolean(self.complete, "target_snapshot.complete")
        _utc(self.captured_at, "target_snapshot.captured_at")
        _safe_id(self.source_identity, "target_snapshot.source_identity")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "request_digest": self.request_digest,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "operation_release_id": self.operation_release_id,
            "candidate_release_id": self.candidate_release_id,
            "target": self.target.value,
            "candidate_artifact_digest": self.candidate_artifact_digest,
            "candidate_manifest_hash": self.candidate_manifest_hash,
            "target_profile_digest": self.target_profile_digest,
            "current_artifact_digest": self.current_artifact_digest,
            "current_manifest_hash": self.current_manifest_hash,
            "target_config_digest": self.target_config_digest,
            "current_schema": self.current_schema,
            "migration_history_digest": self.migration_history_digest,
            "migration_class": self.migration_class.value,
            "schema_compatible": self.schema_compatible,
            "migration_allowed": self.migration_allowed,
            "complete": self.complete,
            "captured_at": _timestamp(self.captured_at),
            "source_identity": self.source_identity,
            "evidence_hash": self.evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "target snapshot receipt")


class VerifiedTargetStateResolverPort(Protocol):
    def snapshot_verified(
        self, request: TargetSnapshotRequest
    ) -> TargetSnapshotReceipt:
        """Return a complete, authenticated target and migration-history snapshot."""


_TARGET_SNAPSHOT_SEAL = object()


@dataclass(frozen=True, init=False)
class TargetSnapshot:
    target: TargetName
    target_profile_digest: str
    expected_current_digest: str
    current_manifest_hash: str
    target_config_digest: str
    current_schema: int
    migration_history_digest: str
    receipt_hash: str
    source_identity: str
    captured_at: datetime

    def __init__(self) -> None:
        raise EvidenceRejected("target snapshot requires a verified resolver")

    @classmethod
    def _from_verified_receipt(
        cls,
        receipt: TargetSnapshotReceipt,
        seal: object,
    ) -> TargetSnapshot:
        if seal is not _TARGET_SNAPSHOT_SEAL:
            raise EvidenceRejected("target snapshot requires a verified resolver")
        snapshot = object.__new__(cls)
        object.__setattr__(snapshot, "target", receipt.target)
        object.__setattr__(
            snapshot, "target_profile_digest", receipt.target_profile_digest
        )
        object.__setattr__(
            snapshot, "expected_current_digest", receipt.current_artifact_digest
        )
        object.__setattr__(
            snapshot, "current_manifest_hash", receipt.current_manifest_hash
        )
        object.__setattr__(
            snapshot,
            "target_config_digest",
            receipt.target_config_digest,
        )
        object.__setattr__(snapshot, "current_schema", receipt.current_schema)
        object.__setattr__(
            snapshot,
            "migration_history_digest",
            receipt.migration_history_digest,
        )
        object.__setattr__(snapshot, "receipt_hash", receipt.receipt_hash)
        object.__setattr__(snapshot, "source_identity", receipt.source_identity)
        object.__setattr__(snapshot, "captured_at", receipt.captured_at)
        return snapshot


def _resolve_target_snapshot(
    *,
    contract: ReleaseControlContract,
    operation_release_id: str,
    manifest: EmbeddedManifest,
    artifact: ArtifactDescriptor,
    target: TargetName,
    resolver: VerifiedTargetStateResolverPort,
    clock: TrustedClockPort,
) -> TargetSnapshot:
    request = TargetSnapshotRequest(
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        operation_release_id=operation_release_id,
        candidate_release_id=manifest.release_id,
        target=target,
        candidate_artifact_digest=artifact.digest,
        candidate_manifest_hash=manifest.manifest_hash,
        migration_class=manifest.migration_class,
        schema_min=manifest.schema_min,
        schema_max=manifest.schema_max,
    )
    started_at = _trusted_now(clock)
    try:
        receipt = resolver.snapshot_verified(request)
    except Exception as exc:
        LOGGER.warning("target resolver failed closed (%s)", type(exc).__name__)
        raise EvidenceRejected("verified target resolver failed") from exc
    now = _trusted_post_call_now(clock, started_at, "target resolution")
    if type(receipt) is not TargetSnapshotReceipt:
        raise EvidenceRejected("target resolver returned an untyped assertion")
    expected = {
        "request_digest": request.request_digest,
        "repository": contract.repository,
        "contract_digest": contract.contract_digest,
        "contract_edition": contract.contract_edition,
        "operation_release_id": operation_release_id,
        "candidate_release_id": manifest.release_id,
        "target": target,
        "candidate_artifact_digest": artifact.digest,
        "candidate_manifest_hash": manifest.manifest_hash,
        "migration_class": manifest.migration_class,
    }
    for field, value in expected.items():
        if getattr(receipt, field) != value:
            raise EvidenceRejected(f"target snapshot mismatch: {field}")
    if (
        not receipt.complete
        or not receipt.schema_compatible
        or not receipt.migration_allowed
    ):
        raise EvidenceRejected("target schema or migration history is not compatible")
    if not manifest.schema_min <= receipt.current_schema <= manifest.schema_max:
        raise EvidenceRejected("target current_schema is outside the manifest range")
    if _age_seconds(receipt.captured_at, now, "target_snapshot.captured_at") > (
        MAX_TARGET_SNAPSHOT_AGE_SECONDS
    ):
        raise EvidenceRejected("target snapshot is stale")
    return TargetSnapshot._from_verified_receipt(receipt, _TARGET_SNAPSHOT_SEAL)


_RELEASE_OPERATION_SEAL = object()


@dataclass(frozen=True)
class ReleaseOperation:
    repository: str
    contract_digest: str
    contract_edition: str
    required_checks: tuple[str, ...]
    release_id: str
    manifest_release_id: str
    action: ReleaseAction
    artifact_digest: str
    image_reference: str
    embedded_manifest_hash: str
    embedded_layer_digest: str
    artifact_receipt_hash: str
    artifact_resolver_identity: str
    check_suite_receipt_hash: str
    source_candidate_receipt_hash: str | None
    pilot_qualification_receipt_hash: str | None
    qualified_pilot_applied_receipt_hash: str | None
    rollback_historical_contract_digest: str | None
    rollback_historical_contract_edition: str | None
    rollback_historical_required_checks_digest: str | None
    rollback_historical_receipt_hash: str | None
    rollback_compatibility_receipt_hash: str | None
    gate_evidence_hash: str
    target: TargetName
    target_profile_digest: str
    target_snapshot_hash: str
    target_source_identity: str
    expected_current_digest: str
    target_config_digest: str
    migration_history_digest: str
    current_schema: int
    migration_class: MigrationClass
    schema_min: int
    schema_max: int
    evidence_valid_until: datetime
    required_pilot_operation_fingerprint: str | None
    _verified_seal: InitVar[object | None] = None
    _planned: bool = dataclass_field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self, _verified_seal: object | None) -> None:
        if _verified_seal is not _RELEASE_OPERATION_SEAL:
            raise EvidenceRejected("operation requires verified planning evidence")
        object.__setattr__(self, "_planned", True)
        _bounded_string(self.repository, "operation.repository", maximum=129)
        _digest(self.contract_digest, "operation.contract_digest")
        _safe_id(self.contract_edition, "operation.contract_edition")
        _check_ids(self.required_checks, "operation.required_checks")
        _safe_id(self.release_id, "operation.release_id")
        _safe_id(self.manifest_release_id, "operation.manifest_release_id")
        if not isinstance(self.action, ReleaseAction):
            raise SchemaError("operation.action must be a ReleaseAction")
        for field in (
            "artifact_digest",
            "embedded_manifest_hash",
            "embedded_layer_digest",
            "artifact_receipt_hash",
            "check_suite_receipt_hash",
            "gate_evidence_hash",
            "target_profile_digest",
            "target_snapshot_hash",
            "expected_current_digest",
            "target_config_digest",
            "migration_history_digest",
        ):
            _digest(getattr(self, field), f"operation.{field}")
        _safe_id(
            self.artifact_resolver_identity,
            "operation.artifact_resolver_identity",
        )
        _safe_id(self.target_source_identity, "operation.target_source_identity")
        validate_immutable_image_reference(self.image_reference, self.artifact_digest)
        if self.action is ReleaseAction.PROMOTE:
            _digest(
                self.source_candidate_receipt_hash,
                "operation.source_candidate_receipt_hash",
            )
            if any(
                value is not None
                for value in (
                    self.rollback_historical_contract_digest,
                    self.rollback_historical_contract_edition,
                    self.rollback_historical_required_checks_digest,
                    self.rollback_historical_receipt_hash,
                    self.rollback_compatibility_receipt_hash,
                )
            ):
                raise SchemaError("promotion cannot carry rollback evidence")
        elif self.source_candidate_receipt_hash is not None:
            raise SchemaError("rollback cannot carry promotion candidate evidence")
        else:
            _digest(
                self.rollback_historical_contract_digest,
                "operation.rollback_historical_contract_digest",
            )
            _safe_id(
                self.rollback_historical_contract_edition,
                "operation.rollback_historical_contract_edition",
            )
            _digest(
                self.rollback_historical_required_checks_digest,
                "operation.rollback_historical_required_checks_digest",
            )
            _digest(
                self.rollback_historical_receipt_hash,
                "operation.rollback_historical_receipt_hash",
            )
            _digest(
                self.rollback_compatibility_receipt_hash,
                "operation.rollback_compatibility_receipt_hash",
            )
        qualification_hashes = (
            self.pilot_qualification_receipt_hash,
            self.qualified_pilot_applied_receipt_hash,
        )
        if self.target is TargetName.PILOT:
            if any(value is not None for value in qualification_hashes):
                raise SchemaError("pilot cannot carry its own qualification evidence")
        elif self.action is ReleaseAction.ROLLBACK:
            if any(value is not None for value in qualification_hashes):
                raise SchemaError(
                    "rollback cannot carry promotion qualification evidence"
                )
        elif all(value is None for value in qualification_hashes):
            pass
        elif any(value is None for value in qualification_hashes):
            raise SchemaError("production qualification evidence is incomplete")
        else:
            _digest(
                self.pilot_qualification_receipt_hash,
                "operation.pilot_qualification_receipt_hash",
            )
            _digest(
                self.qualified_pilot_applied_receipt_hash,
                "operation.qualified_pilot_applied_receipt_hash",
            )
        if not isinstance(self.target, TargetName):
            raise SchemaError("operation.target must be a TargetName")
        if not isinstance(self.migration_class, MigrationClass):
            raise SchemaError("operation.migration_class must be a MigrationClass")
        for field in ("current_schema", "schema_min", "schema_max"):
            _bounded_int(
                getattr(self, field),
                f"operation.{field}",
                minimum=0,
                maximum=2_147_483_647,
            )
        if self.schema_min > self.schema_max:
            raise SchemaError("operation schema range is inverted")
        _utc(self.evidence_valid_until, "operation.evidence_valid_until")
        if self.target is TargetName.PRODUCTION:
            _digest(
                self.required_pilot_operation_fingerprint,
                "operation.required_pilot_operation_fingerprint",
            )
        elif self.required_pilot_operation_fingerprint is not None:
            raise SchemaError("pilot operation cannot require another pilot operation")

    @property
    def required_checks_digest(self) -> str:
        return _hash(list(self.required_checks), "operation.required_checks")

    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "fingerprint_schema_version": 5,
            "repository": self.repository,
            "contract": {
                "digest": self.contract_digest,
                "edition": self.contract_edition,
                "required_checks": list(self.required_checks),
            },
            "release_id": self.release_id,
            "manifest_release_id": self.manifest_release_id,
            "action": self.action.value,
            "artifact": {
                "digest": self.artifact_digest,
                "image_reference": self.image_reference,
                "manifest_hash": self.embedded_manifest_hash,
                "embedded_layer_digest": self.embedded_layer_digest,
                "resolution_receipt_hash": self.artifact_receipt_hash,
                "resolver_identity": self.artifact_resolver_identity,
            },
            "checks": {
                "required_checks": list(self.required_checks),
                "suite_receipt_hash": self.check_suite_receipt_hash,
            },
            "source_candidate_receipt_hash": self.source_candidate_receipt_hash,
            "pilot_qualification_receipt_hash": (self.pilot_qualification_receipt_hash),
            "qualified_pilot_applied_receipt_hash": (
                self.qualified_pilot_applied_receipt_hash
            ),
            "rollback_compatibility": (
                None
                if self.action is ReleaseAction.PROMOTE
                else {
                    "historical_contract_digest": (
                        self.rollback_historical_contract_digest
                    ),
                    "historical_contract_edition": (
                        self.rollback_historical_contract_edition
                    ),
                    "historical_required_checks_digest": (
                        self.rollback_historical_required_checks_digest
                    ),
                    "historical_receipt_hash": (self.rollback_historical_receipt_hash),
                    "compatibility_receipt_hash": (
                        self.rollback_compatibility_receipt_hash
                    ),
                }
            ),
            "gate_evidence_hash": self.gate_evidence_hash,
            "target": {
                "name": self.target.value,
                "profile_digest": self.target_profile_digest,
                "snapshot_hash": self.target_snapshot_hash,
                "source_identity": self.target_source_identity,
                "expected_current_digest": self.expected_current_digest,
                "target_config_digest": self.target_config_digest,
                "migration_history_digest": self.migration_history_digest,
                "current_schema": self.current_schema,
            },
            "migration": {
                "class": self.migration_class.value,
                "schema_min": self.schema_min,
                "schema_max": self.schema_max,
            },
            "evidence_valid_until": _timestamp(self.evidence_valid_until),
            "required_pilot_operation_fingerprint": (
                self.required_pilot_operation_fingerprint
            ),
        }

    @property
    def operation_fingerprint(self) -> str:
        return _hash(self.fingerprint_payload(), "operation fingerprint")


def _operation_from_verified_evidence(
    *,
    contract: ReleaseControlContract,
    operation_release_id: str,
    action: ReleaseAction,
    manifest: EmbeddedManifest,
    artifact: ArtifactDescriptor,
    check_suite: CheckSuiteReceipt,
    source_candidate: SourceCandidateReceipt | None,
    pilot_qualification_receipt_hash: str | None = None,
    qualified_pilot_applied_receipt_hash: str | None = None,
    rollback_historical_contract_digest: str | None = None,
    rollback_historical_contract_edition: str | None = None,
    rollback_historical_required_checks_digest: str | None = None,
    rollback_historical_receipt_hash: str | None = None,
    rollback_compatibility_receipt_hash: str | None = None,
    target: TargetSnapshot,
    evidence_valid_until: datetime | None = None,
    additional_gate_evidence_hash: str | None = None,
    required_pilot_operation_fingerprint: str | None = None,
) -> ReleaseOperation:
    base_valid_until = min(
        _utc(artifact.resolved_at, "artifact resolved_at")
        + timedelta(seconds=MAX_ARTIFACT_RECEIPT_AGE_SECONDS),
        _utc(target.captured_at, "target captured_at")
        + timedelta(seconds=MAX_TARGET_SNAPSHOT_AGE_SECONDS),
    )
    valid_until = min(base_valid_until, evidence_valid_until or base_valid_until)
    gate_evidence = {
        "domain": "iwe.release-control.gate-evidence.v1",
        "action": action.value,
        "artifact_receipt_hash": artifact.resolution_receipt_hash,
        "check_suite_receipt_hash": check_suite.receipt_hash,
        "target_snapshot_hash": target.receipt_hash,
    }
    if source_candidate is not None:
        gate_evidence["source_candidate_receipt_hash"] = source_candidate.receipt_hash
    if additional_gate_evidence_hash is not None:
        gate_evidence["additional_evidence_hash"] = _digest(
            additional_gate_evidence_hash,
            "additional gate evidence",
        )
    return ReleaseOperation(
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        required_checks=contract.required_checks,
        release_id=operation_release_id,
        manifest_release_id=manifest.release_id,
        action=action,
        artifact_digest=artifact.digest,
        image_reference=artifact.image_reference,
        embedded_manifest_hash=manifest.manifest_hash,
        embedded_layer_digest=artifact.embedded_layer_digest,
        artifact_receipt_hash=artifact.resolution_receipt_hash,
        artifact_resolver_identity=artifact.resolver_identity,
        check_suite_receipt_hash=check_suite.receipt_hash,
        source_candidate_receipt_hash=(
            source_candidate.receipt_hash if source_candidate is not None else None
        ),
        pilot_qualification_receipt_hash=pilot_qualification_receipt_hash,
        qualified_pilot_applied_receipt_hash=(qualified_pilot_applied_receipt_hash),
        rollback_historical_contract_digest=(rollback_historical_contract_digest),
        rollback_historical_contract_edition=(rollback_historical_contract_edition),
        rollback_historical_required_checks_digest=(
            rollback_historical_required_checks_digest
        ),
        rollback_historical_receipt_hash=rollback_historical_receipt_hash,
        rollback_compatibility_receipt_hash=(rollback_compatibility_receipt_hash),
        gate_evidence_hash=_hash(gate_evidence, "operation gate evidence"),
        target=target.target,
        target_profile_digest=target.target_profile_digest,
        target_snapshot_hash=target.receipt_hash,
        target_source_identity=target.source_identity,
        expected_current_digest=target.expected_current_digest,
        target_config_digest=target.target_config_digest,
        migration_history_digest=target.migration_history_digest,
        current_schema=target.current_schema,
        migration_class=manifest.migration_class,
        schema_min=manifest.schema_min,
        schema_max=manifest.schema_max,
        evidence_valid_until=valid_until,
        required_pilot_operation_fingerprint=required_pilot_operation_fingerprint,
        _verified_seal=_RELEASE_OPERATION_SEAL,
    )


_PROMOTION_PLAN_SEAL = object()


@dataclass(frozen=True)
class PromotionPlan:
    release_id: str
    artifact_digest: str
    embedded_manifest_hash: str
    contract_digest: str
    contract_edition: str
    required_checks: tuple[str, ...]
    operations: tuple[ReleaseOperation, ReleaseOperation]
    _manifest: EmbeddedManifest = dataclass_field(repr=False)
    _artifact: ArtifactDescriptor = dataclass_field(repr=False)
    _check_suite: CheckSuiteReceipt = dataclass_field(repr=False)
    _source_candidate: SourceCandidateReceipt = dataclass_field(repr=False)
    _pilot_target: TargetSnapshot = dataclass_field(repr=False)
    _production_target: TargetSnapshot = dataclass_field(repr=False)
    _plan_seal: InitVar[object | None] = None
    _validated: bool = dataclass_field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self, _plan_seal: object | None) -> None:
        if _plan_seal is not _PROMOTION_PLAN_SEAL:
            raise EvidenceRejected("promotion plan requires verified evidence")
        object.__setattr__(self, "_validated", True)

    @property
    def artifact_build_count(self) -> int:
        return 1


def plan_build_once_promotion(
    *,
    contract: ReleaseControlContract,
    manifest: EmbeddedManifest,
    artifact_resolver: VerifiedArtifactResolverPort,
    check_suite_resolver: VerifiedCheckSuiteResolverPort,
    source_candidate_resolver: VerifiedSourceCandidateResolverPort,
    target_resolver: VerifiedTargetStateResolverPort,
    clock: TrustedClockPort,
) -> PromotionPlan:
    """Plan pilot→production from one verified linux/amd64 OCI manifest."""

    manifest = _validate_manifest(manifest, contract)
    clock = _MonotonicTrustedClock(clock)
    artifact = _resolve_artifact(
        contract=contract,
        manifest=manifest,
        purpose=ArtifactPurpose.PROMOTION,
        resolver=artifact_resolver,
        clock=clock,
    )
    check_suite = _resolve_check_suite(
        contract=contract,
        manifest=manifest,
        artifact=artifact,
        resolver=check_suite_resolver,
        clock=clock,
    )
    source_candidate = _resolve_source_candidate(
        contract=contract,
        manifest=manifest,
        artifact=artifact,
        check_suite=check_suite,
        resolver=source_candidate_resolver,
        clock=clock,
    )
    promotion_evidence_valid_until = min(
        _check_suite_valid_until(
            check_suite,
            max_age_seconds=contract.check_suite_max_age_seconds,
        ),
        _utc(source_candidate.verified_at, "source_candidate.verified_at")
        + timedelta(seconds=contract.source_candidate_max_age_seconds),
    )
    pilot = _resolve_target_snapshot(
        contract=contract,
        operation_release_id=manifest.release_id,
        manifest=manifest,
        artifact=artifact,
        target=TargetName.PILOT,
        resolver=target_resolver,
        clock=clock,
    )
    production = _resolve_target_snapshot(
        contract=contract,
        operation_release_id=manifest.release_id,
        manifest=manifest,
        artifact=artifact,
        target=TargetName.PRODUCTION,
        resolver=target_resolver,
        clock=clock,
    )
    pilot_operation = _operation_from_verified_evidence(
        contract=contract,
        operation_release_id=manifest.release_id,
        action=ReleaseAction.PROMOTE,
        manifest=manifest,
        artifact=artifact,
        check_suite=check_suite,
        source_candidate=source_candidate,
        target=pilot,
        evidence_valid_until=promotion_evidence_valid_until,
    )
    production_operation = _operation_from_verified_evidence(
        contract=contract,
        operation_release_id=manifest.release_id,
        action=ReleaseAction.PROMOTE,
        manifest=manifest,
        artifact=artifact,
        check_suite=check_suite,
        source_candidate=source_candidate,
        target=production,
        evidence_valid_until=promotion_evidence_valid_until,
        required_pilot_operation_fingerprint=(pilot_operation.operation_fingerprint),
    )
    operations = (pilot_operation, production_operation)
    if len({operation.artifact_digest for operation in operations}) != 1:
        raise EvidenceRejected("pilot and production do not share one artifact digest")
    return PromotionPlan(
        release_id=manifest.release_id,
        artifact_digest=artifact.digest,
        embedded_manifest_hash=manifest.manifest_hash,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        required_checks=contract.required_checks,
        operations=operations,
        _manifest=manifest,
        _artifact=artifact,
        _check_suite=check_suite,
        _source_candidate=source_candidate,
        _pilot_target=pilot,
        _production_target=production,
        _plan_seal=_PROMOTION_PLAN_SEAL,
    )


@dataclass(frozen=True)
class HistoricalBuildContractRequest:
    repository: str
    historical_build_contract_digest: str
    previous_release_id: str
    previous_manifest_hash: str
    source_commit: str
    source_tree: str
    artifact_digest: str
    historical_required_checks: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "historical_build_contract_digest": (self.historical_build_contract_digest),
            "previous_release_id": self.previous_release_id,
            "previous_manifest_hash": self.previous_manifest_hash,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "artifact_digest": self.artifact_digest,
            "historical_required_checks": list(self.historical_required_checks),
        }

    @property
    def request_digest(self) -> str:
        return _hash(self.to_mapping(), "historical build contract request")


@dataclass(frozen=True)
class HistoricalBuildContractReceipt:
    schema_version: int
    receipt_id: str
    request_digest: str
    repository: str
    historical_build_contract_digest: str
    historical_contract_edition: str
    previous_release_id: str
    previous_manifest_hash: str
    source_commit: str
    source_tree: str
    artifact_digest: str
    historical_required_checks: tuple[str, ...]
    archive_complete: bool
    immutable: bool
    verifier_identity: str
    sealed_at: datetime
    archive_reference_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "historical_contract.schema_version")
        _canonical_uuid(self.receipt_id, "historical_contract.receipt_id")
        _bounded_string(
            self.repository,
            "historical_contract.repository",
            maximum=129,
        )
        for field_name in (
            "request_digest",
            "historical_build_contract_digest",
            "previous_manifest_hash",
            "artifact_digest",
            "archive_reference_hash",
            "evidence_hash",
        ):
            _digest(getattr(self, field_name), f"historical_contract.{field_name}")
        _safe_id(
            self.historical_contract_edition,
            "historical_contract.historical_contract_edition",
        )
        _safe_id(self.previous_release_id, "historical_contract.previous_release_id")
        _git_oid(self.source_commit, "historical_contract.source_commit")
        _git_oid(self.source_tree, "historical_contract.source_tree")
        _check_ids(
            self.historical_required_checks,
            "historical_contract.historical_required_checks",
        )
        _boolean(self.archive_complete, "historical_contract.archive_complete")
        _boolean(self.immutable, "historical_contract.immutable")
        _safe_id(self.verifier_identity, "historical_contract.verifier_identity")
        _utc(self.sealed_at, "historical_contract.sealed_at")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "request_digest": self.request_digest,
            "repository": self.repository,
            "historical_build_contract_digest": (self.historical_build_contract_digest),
            "historical_contract_edition": self.historical_contract_edition,
            "previous_release_id": self.previous_release_id,
            "previous_manifest_hash": self.previous_manifest_hash,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "artifact_digest": self.artifact_digest,
            "historical_required_checks": list(self.historical_required_checks),
            "archive_complete": self.archive_complete,
            "immutable": self.immutable,
            "verifier_identity": self.verifier_identity,
            "sealed_at": _timestamp(self.sealed_at),
            "archive_reference_hash": self.archive_reference_hash,
            "evidence_hash": self.evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "historical build contract receipt")


class VerifiedHistoricalBuildContractResolverPort(Protocol):
    def resolve_verified(
        self,
        request: HistoricalBuildContractRequest,
    ) -> HistoricalBuildContractReceipt:
        """Verify one immutable archived build contract for the old artifact."""


def _resolve_historical_build_contract(
    *,
    contract: ReleaseControlContract,
    manifest: EmbeddedManifest,
    artifact: ArtifactDescriptor,
    resolver: VerifiedHistoricalBuildContractResolverPort,
    clock: TrustedClockPort,
) -> HistoricalBuildContractReceipt:
    request = HistoricalBuildContractRequest(
        repository=contract.repository,
        historical_build_contract_digest=manifest.build_contract_digest,
        previous_release_id=manifest.release_id,
        previous_manifest_hash=manifest.manifest_hash,
        source_commit=manifest.source_commit,
        source_tree=manifest.source_tree,
        artifact_digest=artifact.digest,
        historical_required_checks=tuple(manifest.required_checks),
    )
    started_at = _trusted_now(clock)
    try:
        receipt = resolver.resolve_verified(request)
    except Exception as exc:
        LOGGER.warning(
            "historical contract resolver failed closed (%s)",
            type(exc).__name__,
        )
        raise RollbackRejected("verified historical build contract failed") from exc
    now = _trusted_post_call_now(
        clock,
        started_at,
        "historical contract resolution",
    )
    if type(receipt) is not HistoricalBuildContractReceipt:
        raise RollbackRejected("historical contract resolver returned untyped evidence")
    expected = {
        "request_digest": request.request_digest,
        "repository": request.repository,
        "historical_build_contract_digest": (request.historical_build_contract_digest),
        "previous_release_id": request.previous_release_id,
        "previous_manifest_hash": request.previous_manifest_hash,
        "source_commit": request.source_commit,
        "source_tree": request.source_tree,
        "artifact_digest": request.artifact_digest,
        "historical_required_checks": request.historical_required_checks,
        "archive_complete": True,
        "immutable": True,
    }
    for field_name, expected_value in expected.items():
        if getattr(receipt, field_name) != expected_value:
            raise RollbackRejected(
                f"historical contract receipt mismatch: {field_name}"
            )
    try:
        _age_seconds(receipt.sealed_at, now, "historical_contract.sealed_at")
    except EvidenceRejected as exc:
        raise RollbackRejected(str(exc)) from exc
    return receipt


@dataclass(frozen=True)
class RollbackCapabilityRequest:
    repository: str
    contract_digest: str
    contract_edition: str
    rollback_release_id: str
    previous_release_id: str
    target: TargetName
    previous_good_digest: str
    previous_manifest_hash: str
    artifact_receipt_hash: str
    expected_current_digest: str
    target_snapshot_hash: str
    current_schema: int
    migration_history_digest: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "rollback_release_id": self.rollback_release_id,
            "previous_release_id": self.previous_release_id,
            "target": self.target.value,
            "previous_good_digest": self.previous_good_digest,
            "previous_manifest_hash": self.previous_manifest_hash,
            "artifact_receipt_hash": self.artifact_receipt_hash,
            "expected_current_digest": self.expected_current_digest,
            "target_snapshot_hash": self.target_snapshot_hash,
            "current_schema": self.current_schema,
            "migration_history_digest": self.migration_history_digest,
        }

    @property
    def request_digest(self) -> str:
        return _hash(self.to_mapping(), "rollback capability request")


@dataclass(frozen=True)
class RollbackCapabilityReceipt:
    schema_version: int
    receipt_id: str
    request_digest: str
    previous_good_digest: str
    previous_manifest_hash: str
    target: TargetName
    target_snapshot_hash: str
    expected_current_digest: str
    current_schema: int
    migration_history_digest: str
    can_rollback: bool
    can_rollback_checked_at: datetime
    artifact_retained: bool
    retention_checked_at: datetime
    artifact_origin: str
    runtime_attestation_valid: bool
    runtime_reattested_at: datetime
    pilot_operation_fingerprint: str
    source_identity: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "rollback_receipt.schema_version")
        _canonical_uuid(self.receipt_id, "rollback_receipt.receipt_id")
        for field in (
            "request_digest",
            "previous_good_digest",
            "previous_manifest_hash",
            "target_snapshot_hash",
            "expected_current_digest",
            "migration_history_digest",
            "pilot_operation_fingerprint",
            "evidence_hash",
        ):
            _digest(getattr(self, field), f"rollback_receipt.{field}")
        if not isinstance(self.target, TargetName):
            raise SchemaError("rollback_receipt.target must be a TargetName")
        _bounded_int(
            self.current_schema,
            "rollback_receipt.current_schema",
            minimum=0,
            maximum=2_147_483_647,
        )
        _boolean(self.can_rollback, "rollback_receipt.can_rollback")
        _utc(self.can_rollback_checked_at, "rollback_receipt.can_rollback_checked_at")
        _boolean(self.artifact_retained, "rollback_receipt.artifact_retained")
        _utc(self.retention_checked_at, "rollback_receipt.retention_checked_at")
        _bounded_string(
            self.artifact_origin,
            "rollback_receipt.artifact_origin",
            maximum=32,
        )
        _boolean(
            self.runtime_attestation_valid,
            "rollback_receipt.runtime_attestation_valid",
        )
        _utc(self.runtime_reattested_at, "rollback_receipt.runtime_reattested_at")
        _safe_id(self.source_identity, "rollback_receipt.source_identity")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "request_digest": self.request_digest,
            "previous_good_digest": self.previous_good_digest,
            "previous_manifest_hash": self.previous_manifest_hash,
            "target": self.target.value,
            "target_snapshot_hash": self.target_snapshot_hash,
            "expected_current_digest": self.expected_current_digest,
            "current_schema": self.current_schema,
            "migration_history_digest": self.migration_history_digest,
            "can_rollback": self.can_rollback,
            "can_rollback_checked_at": _timestamp(self.can_rollback_checked_at),
            "artifact_retained": self.artifact_retained,
            "retention_checked_at": _timestamp(self.retention_checked_at),
            "artifact_origin": self.artifact_origin,
            "runtime_attestation_valid": self.runtime_attestation_valid,
            "runtime_reattested_at": _timestamp(self.runtime_reattested_at),
            "pilot_operation_fingerprint": self.pilot_operation_fingerprint,
            "source_identity": self.source_identity,
            "evidence_hash": self.evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "rollback capability receipt")


class VerifiedRollbackResolverPort(Protocol):
    def resolve_verified(
        self,
        request: RollbackCapabilityRequest,
    ) -> RollbackCapabilityReceipt:
        """Verify retained artifact, canRollback, runtime, schema, and history."""


def _validate_rollback_receipt(
    *,
    contract: ReleaseControlContract,
    request: RollbackCapabilityRequest,
    receipt: RollbackCapabilityReceipt,
    now: datetime,
) -> None:
    expected = {
        "request_digest": request.request_digest,
        "previous_good_digest": request.previous_good_digest,
        "previous_manifest_hash": request.previous_manifest_hash,
        "target": request.target,
        "target_snapshot_hash": request.target_snapshot_hash,
        "expected_current_digest": request.expected_current_digest,
        "current_schema": request.current_schema,
        "migration_history_digest": request.migration_history_digest,
    }
    for field, value in expected.items():
        if getattr(receipt, field) != value:
            raise RollbackRejected(f"rollback evidence mismatch: {field}")
    if not receipt.can_rollback:
        raise RollbackRejected("provider no longer reports canRollback")
    if not receipt.artifact_retained or receipt.artifact_origin != "retained":
        raise RollbackRejected(
            "previous-good artifact is not retained; rebuild is forbidden"
        )
    if not receipt.runtime_attestation_valid:
        raise RollbackRejected("runtime attestation is invalid")
    try:
        can_rollback_age = _age_seconds(
            receipt.can_rollback_checked_at,
            now,
            "canRollback observation",
        )
        retention_age = _age_seconds(
            receipt.retention_checked_at,
            now,
            "retention observation",
        )
        reattestation_age = _age_seconds(
            receipt.runtime_reattested_at,
            now,
            "runtime re-attestation",
        )
    except EvidenceRejected as exc:
        raise RollbackRejected(str(exc)) from exc
    if can_rollback_age > contract.rollback_retention_recheck_seconds:
        raise RollbackRejected("canRollback observation is stale")
    if retention_age > contract.rollback_retention_recheck_seconds:
        raise RollbackRejected("retention observation is stale")
    if reattestation_age > contract.rollback_reattestation_seconds:
        raise RollbackRejected("runtime re-attestation is stale")


@dataclass(frozen=True)
class RollbackCompatibilityRequest:
    repository: str
    current_contract_digest: str
    current_contract_edition: str
    historical_contract_digest: str
    historical_contract_edition: str
    historical_contract_receipt_hash: str
    rollback_release_id: str
    previous_release_id: str
    artifact_digest: str
    embedded_manifest_hash: str
    migration_class: MigrationClass
    schema_min: int
    schema_max: int
    target_snapshot_hash: str
    target_config_digest: str
    current_schema: int
    migration_history_digest: str
    rollback_capability_receipt_hash: str
    excluded_verifier_identities: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "current_contract_digest": self.current_contract_digest,
            "current_contract_edition": self.current_contract_edition,
            "historical_contract_digest": self.historical_contract_digest,
            "historical_contract_edition": self.historical_contract_edition,
            "historical_contract_receipt_hash": (self.historical_contract_receipt_hash),
            "rollback_release_id": self.rollback_release_id,
            "previous_release_id": self.previous_release_id,
            "artifact_digest": self.artifact_digest,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "migration": {
                "class": self.migration_class.value,
                "schema_min": self.schema_min,
                "schema_max": self.schema_max,
                "current_schema": self.current_schema,
                "history_digest": self.migration_history_digest,
            },
            "target_snapshot_hash": self.target_snapshot_hash,
            "target_config_digest": self.target_config_digest,
            "rollback_capability_receipt_hash": (self.rollback_capability_receipt_hash),
            "excluded_verifier_identities": list(self.excluded_verifier_identities),
        }

    @property
    def request_digest(self) -> str:
        return _hash(self.to_mapping(), "rollback compatibility request")


@dataclass(frozen=True)
class RollbackCompatibilityReceipt:
    schema_version: int
    receipt_id: str
    request_digest: str
    current_contract_digest: str
    current_contract_edition: str
    historical_contract_digest: str
    historical_contract_edition: str
    historical_contract_receipt_hash: str
    rollback_release_id: str
    previous_release_id: str
    artifact_digest: str
    embedded_manifest_hash: str
    migration_class: MigrationClass
    schema_min: int
    schema_max: int
    target_snapshot_hash: str
    target_config_digest: str
    current_schema: int
    migration_history_digest: str
    rollback_capability_receipt_hash: str
    compatible: bool
    complete: bool
    independent_verifier: bool
    verifier_identity: str
    verified_at: datetime
    external_receipt_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "rollback_compatibility.schema_version")
        _canonical_uuid(self.receipt_id, "rollback_compatibility.receipt_id")
        for field_name in (
            "request_digest",
            "current_contract_digest",
            "historical_contract_digest",
            "historical_contract_receipt_hash",
            "artifact_digest",
            "embedded_manifest_hash",
            "target_snapshot_hash",
            "target_config_digest",
            "migration_history_digest",
            "rollback_capability_receipt_hash",
            "external_receipt_hash",
            "evidence_hash",
        ):
            _digest(getattr(self, field_name), f"rollback_compatibility.{field_name}")
        _safe_id(
            self.current_contract_edition,
            "rollback_compatibility.current_contract_edition",
        )
        _safe_id(
            self.historical_contract_edition,
            "rollback_compatibility.historical_contract_edition",
        )
        _safe_id(
            self.rollback_release_id,
            "rollback_compatibility.rollback_release_id",
        )
        _safe_id(
            self.previous_release_id,
            "rollback_compatibility.previous_release_id",
        )
        if not isinstance(self.migration_class, MigrationClass):
            raise SchemaError("rollback compatibility migration class is untyped")
        for field_name in ("schema_min", "schema_max", "current_schema"):
            _bounded_int(
                getattr(self, field_name),
                f"rollback_compatibility.{field_name}",
                minimum=0,
                maximum=2_147_483_647,
            )
        _boolean(self.compatible, "rollback_compatibility.compatible")
        _boolean(self.complete, "rollback_compatibility.complete")
        _boolean(
            self.independent_verifier,
            "rollback_compatibility.independent_verifier",
        )
        _safe_id(
            self.verifier_identity,
            "rollback_compatibility.verifier_identity",
        )
        _utc(self.verified_at, "rollback_compatibility.verified_at")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "request_digest": self.request_digest,
            "current_contract_digest": self.current_contract_digest,
            "current_contract_edition": self.current_contract_edition,
            "historical_contract_digest": self.historical_contract_digest,
            "historical_contract_edition": self.historical_contract_edition,
            "historical_contract_receipt_hash": (self.historical_contract_receipt_hash),
            "rollback_release_id": self.rollback_release_id,
            "previous_release_id": self.previous_release_id,
            "artifact_digest": self.artifact_digest,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "migration_class": self.migration_class.value,
            "schema_min": self.schema_min,
            "schema_max": self.schema_max,
            "target_snapshot_hash": self.target_snapshot_hash,
            "target_config_digest": self.target_config_digest,
            "current_schema": self.current_schema,
            "migration_history_digest": self.migration_history_digest,
            "rollback_capability_receipt_hash": (self.rollback_capability_receipt_hash),
            "compatible": self.compatible,
            "complete": self.complete,
            "independent_verifier": self.independent_verifier,
            "verifier_identity": self.verifier_identity,
            "verified_at": _timestamp(self.verified_at),
            "external_receipt_hash": self.external_receipt_hash,
            "evidence_hash": self.evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "rollback compatibility receipt")


class VerifiedRollbackCompatibilityResolverPort(Protocol):
    def resolve_verified(
        self,
        request: RollbackCompatibilityRequest,
    ) -> RollbackCompatibilityReceipt:
        """Verify historical artifact compatibility under current rollback policy."""


def _resolve_rollback_compatibility(
    *,
    contract: ReleaseControlContract,
    request: RollbackCompatibilityRequest,
    resolver: VerifiedRollbackCompatibilityResolverPort,
    clock: TrustedClockPort,
) -> RollbackCompatibilityReceipt:
    started_at = _trusted_now(clock)
    try:
        receipt = resolver.resolve_verified(request)
    except Exception as exc:
        LOGGER.warning(
            "rollback compatibility resolver failed closed (%s)",
            type(exc).__name__,
        )
        raise RollbackRejected("verified rollback compatibility failed") from exc
    now = _trusted_post_call_now(
        clock,
        started_at,
        "rollback compatibility resolution",
    )
    if type(receipt) is not RollbackCompatibilityReceipt:
        raise RollbackRejected("rollback compatibility returned untyped evidence")
    expected = {
        "request_digest": request.request_digest,
        "current_contract_digest": request.current_contract_digest,
        "current_contract_edition": request.current_contract_edition,
        "historical_contract_digest": request.historical_contract_digest,
        "historical_contract_edition": request.historical_contract_edition,
        "historical_contract_receipt_hash": (request.historical_contract_receipt_hash),
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
        "rollback_capability_receipt_hash": (request.rollback_capability_receipt_hash),
        "compatible": True,
        "complete": True,
        "independent_verifier": True,
    }
    for field_name, expected_value in expected.items():
        if getattr(receipt, field_name) != expected_value:
            raise RollbackRejected(f"rollback compatibility mismatch: {field_name}")
    if receipt.verifier_identity in request.excluded_verifier_identities:
        raise RollbackRejected("rollback compatibility verifier is not independent")
    try:
        age = _age_seconds(
            receipt.verified_at,
            now,
            "rollback_compatibility.verified_at",
        )
    except EvidenceRejected as exc:
        raise RollbackRejected(str(exc)) from exc
    if age > contract.rollback_compatibility_max_age_seconds:
        raise RollbackRejected("rollback compatibility receipt is stale")
    return receipt


def plan_rollback(
    *,
    contract: ReleaseControlContract,
    rollback_release_id: str,
    previous_manifest: EmbeddedManifest,
    artifact_resolver: VerifiedArtifactResolverPort,
    historical_contract_resolver: VerifiedHistoricalBuildContractResolverPort,
    check_suite_resolver: VerifiedCheckSuiteResolverPort,
    target_resolver: VerifiedTargetStateResolverPort,
    rollback_resolver: VerifiedRollbackResolverPort,
    compatibility_resolver: VerifiedRollbackCompatibilityResolverPort,
    clock: TrustedClockPort,
) -> ReleaseOperation:
    """Plan a no-rebuild rollback from mutually bound, fresh receipts."""

    rollback_release_id = _safe_id(rollback_release_id, "rollback_release_id")
    manifest = _canonical_manifest(previous_manifest)
    clock = _MonotonicTrustedClock(clock)
    artifact = _resolve_artifact(
        contract=contract,
        manifest=manifest,
        purpose=ArtifactPurpose.ROLLBACK,
        resolver=artifact_resolver,
        clock=clock,
    )
    historical_contract = _resolve_historical_build_contract(
        contract=contract,
        manifest=manifest,
        artifact=artifact,
        resolver=historical_contract_resolver,
        clock=clock,
    )
    check_suite = _resolve_check_suite(
        contract=contract,
        manifest=manifest,
        artifact=artifact,
        resolver=check_suite_resolver,
        clock=clock,
    )
    check_suite_valid_until = _check_suite_valid_until(
        check_suite,
        max_age_seconds=contract.check_suite_max_age_seconds,
    )
    target = _resolve_target_snapshot(
        contract=contract,
        operation_release_id=rollback_release_id,
        manifest=manifest,
        artifact=artifact,
        target=TargetName.PRODUCTION,
        resolver=target_resolver,
        clock=clock,
    )
    request = RollbackCapabilityRequest(
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        rollback_release_id=rollback_release_id,
        previous_release_id=manifest.release_id,
        target=TargetName.PRODUCTION,
        previous_good_digest=artifact.digest,
        previous_manifest_hash=manifest.manifest_hash,
        artifact_receipt_hash=artifact.resolution_receipt_hash,
        expected_current_digest=target.expected_current_digest,
        target_snapshot_hash=target.receipt_hash,
        current_schema=target.current_schema,
        migration_history_digest=target.migration_history_digest,
    )
    rollback_started_at = _trusted_now(clock)
    try:
        receipt = rollback_resolver.resolve_verified(request)
    except Exception as exc:
        LOGGER.warning("rollback resolver failed closed (%s)", type(exc).__name__)
        raise RollbackRejected("verified rollback resolver failed") from exc
    rollback_resolved_at = _trusted_post_call_now(
        clock,
        rollback_started_at,
        "rollback capability resolution",
    )
    if type(receipt) is not RollbackCapabilityReceipt:
        raise RollbackRejected("rollback resolver returned an untyped assertion")
    _validate_rollback_receipt(
        contract=contract,
        request=request,
        receipt=receipt,
        now=rollback_resolved_at,
    )
    rollback_evidence_identities = {
        artifact.resolver_identity,
        historical_contract.verifier_identity,
        check_suite.verifier_identity,
        target.source_identity,
        receipt.source_identity,
    }
    if len(rollback_evidence_identities) != 5:
        raise RollbackRejected("rollback evidence verifiers are not independent")
    compatibility_request = RollbackCompatibilityRequest(
        repository=contract.repository,
        current_contract_digest=contract.contract_digest,
        current_contract_edition=contract.contract_edition,
        historical_contract_digest=manifest.build_contract_digest,
        historical_contract_edition=(historical_contract.historical_contract_edition),
        historical_contract_receipt_hash=historical_contract.receipt_hash,
        rollback_release_id=rollback_release_id,
        previous_release_id=manifest.release_id,
        artifact_digest=artifact.digest,
        embedded_manifest_hash=manifest.manifest_hash,
        migration_class=manifest.migration_class,
        schema_min=manifest.schema_min,
        schema_max=manifest.schema_max,
        target_snapshot_hash=target.receipt_hash,
        target_config_digest=target.target_config_digest,
        current_schema=target.current_schema,
        migration_history_digest=target.migration_history_digest,
        rollback_capability_receipt_hash=receipt.receipt_hash,
        excluded_verifier_identities=tuple(sorted(rollback_evidence_identities)),
    )
    compatibility = _resolve_rollback_compatibility(
        contract=contract,
        request=compatibility_request,
        resolver=compatibility_resolver,
        clock=clock,
    )
    rollback_evidence_valid_until = min(
        check_suite_valid_until,
        _utc(receipt.can_rollback_checked_at, "canRollback observation")
        + timedelta(seconds=contract.rollback_retention_recheck_seconds),
        _utc(receipt.retention_checked_at, "retention observation")
        + timedelta(seconds=contract.rollback_retention_recheck_seconds),
        _utc(receipt.runtime_reattested_at, "runtime re-attestation")
        + timedelta(seconds=contract.rollback_reattestation_seconds),
        _utc(compatibility.verified_at, "rollback compatibility verification")
        + timedelta(seconds=contract.rollback_compatibility_max_age_seconds),
    )
    rollback_gate_hash = _hash(
        {
            "domain": "iwe.release-control.rollback-gate.v1",
            "historical_contract_receipt_hash": historical_contract.receipt_hash,
            "rollback_capability_receipt_hash": receipt.receipt_hash,
            "rollback_compatibility_receipt_hash": compatibility.receipt_hash,
        },
        "rollback gate evidence",
    )
    return _operation_from_verified_evidence(
        contract=contract,
        operation_release_id=rollback_release_id,
        action=ReleaseAction.ROLLBACK,
        manifest=manifest,
        artifact=artifact,
        check_suite=check_suite,
        source_candidate=None,
        rollback_historical_contract_digest=manifest.build_contract_digest,
        rollback_historical_contract_edition=(
            historical_contract.historical_contract_edition
        ),
        rollback_historical_required_checks_digest=_hash(
            list(historical_contract.historical_required_checks),
            "operation.required_checks",
        ),
        rollback_historical_receipt_hash=historical_contract.receipt_hash,
        rollback_compatibility_receipt_hash=compatibility.receipt_hash,
        target=target,
        evidence_valid_until=rollback_evidence_valid_until,
        additional_gate_evidence_hash=rollback_gate_hash,
        required_pilot_operation_fingerprint=(receipt.pilot_operation_fingerprint),
    )


@dataclass(frozen=True)
class AuthorityEvidence:
    schema_version: int
    purpose: str
    audience: str
    repository: str
    target: TargetName
    contract_digest: str
    contract_edition: str
    required_checks_digest: str
    authority_id: str
    release_id: str
    operation_fingerprint: str
    approver_identity: str
    runner_identity: str
    issued_at: datetime
    expires_at: datetime
    max_uses: int
    nonce: str
    key_id: str
    signature: str

    @classmethod
    def from_mapping(cls, payload: Any) -> AuthorityEvidence:
        obj = _exact_keys(
            payload,
            {
                "schema_version",
                "purpose",
                "audience",
                "repository",
                "target",
                "contract_digest",
                "contract_edition",
                "required_checks_digest",
                "authority_id",
                "release_id",
                "operation_fingerprint",
                "approver_identity",
                "runner_identity",
                "issued_at",
                "expires_at",
                "max_uses",
                "nonce",
                "key_id",
                "signature",
            },
            "authority",
        )
        try:
            target = TargetName(obj["target"])
        except (TypeError, ValueError) as exc:
            raise SchemaError("authority.target is unsupported") from exc
        return cls(
            schema_version=_bounded_int(
                obj["schema_version"], "authority.schema_version", minimum=1, maximum=1
            ),
            purpose=_bounded_string(obj["purpose"], "authority.purpose", maximum=64),
            audience=_bounded_string(obj["audience"], "authority.audience", maximum=64),
            repository=_bounded_string(
                obj["repository"], "authority.repository", maximum=129
            ),
            target=target,
            contract_digest=_digest(
                obj["contract_digest"], "authority.contract_digest"
            ),
            contract_edition=_safe_id(
                obj["contract_edition"], "authority.contract_edition"
            ),
            required_checks_digest=_digest(
                obj["required_checks_digest"], "authority.required_checks_digest"
            ),
            authority_id=_canonical_uuid(obj["authority_id"], "authority.authority_id"),
            release_id=_safe_id(obj["release_id"], "authority.release_id"),
            operation_fingerprint=_digest(
                obj["operation_fingerprint"], "authority.operation_fingerprint"
            ),
            approver_identity=_safe_id(
                obj["approver_identity"], "authority.approver_identity"
            ),
            runner_identity=_safe_id(
                obj["runner_identity"], "authority.runner_identity"
            ),
            issued_at=_parse_utc(obj["issued_at"], "authority.issued_at"),
            expires_at=_parse_utc(obj["expires_at"], "authority.expires_at"),
            max_uses=_bounded_int(
                obj["max_uses"], "authority.max_uses", minimum=1, maximum=1
            ),
            nonce=_safe_id(obj["nonce"], "authority.nonce"),
            key_id=_safe_id(obj["key_id"], "authority.key_id"),
            signature=_bounded_string(
                obj["signature"], "authority.signature", maximum=512
            ),
        )

    def signed_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "purpose": self.purpose,
            "audience": self.audience,
            "repository": self.repository,
            "target": self.target.value,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "required_checks_digest": self.required_checks_digest,
            "authority_id": self.authority_id,
            "release_id": self.release_id,
            "operation_fingerprint": self.operation_fingerprint,
            "approver_identity": self.approver_identity,
            "runner_identity": self.runner_identity,
            "issued_at": _timestamp(self.issued_at),
            "expires_at": _timestamp(self.expires_at),
            "max_uses": self.max_uses,
            "nonce": self.nonce,
            "key_id": self.key_id,
        }

    @property
    def evidence_hash(self) -> str:
        return _hash(
            {"signed_payload": self.signed_payload(), "signature": self.signature},
            "authority evidence",
        )


@dataclass(frozen=True)
class AuthorityVerificationReceipt:
    schema_version: int
    key_id: str
    authority_id: str
    signer_identity: str
    runner_identity: str
    signed_payload_digest: str
    trust_root_identity: str
    signature_valid: bool
    independent_signer: bool
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "authority_verification.schema_version")
        _safe_id(self.key_id, "authority_verification.key_id")
        _canonical_uuid(self.authority_id, "authority_verification.authority_id")
        _safe_id(self.signer_identity, "authority_verification.signer_identity")
        _safe_id(self.runner_identity, "authority_verification.runner_identity")
        _digest(
            self.signed_payload_digest,
            "authority_verification.signed_payload_digest",
        )
        _safe_id(
            self.trust_root_identity,
            "authority_verification.trust_root_identity",
        )
        _boolean(self.signature_valid, "authority_verification.signature_valid")
        _boolean(self.independent_signer, "authority_verification.independent_signer")
        _digest(self.evidence_hash, "authority_verification.evidence_hash")


class AuthorityVerifierPort(Protocol):
    def verify(
        self,
        *,
        key_id: str,
        signed_payload: bytes,
        signature: str,
    ) -> AuthorityVerificationReceipt:
        """Verify the signature and attest the actual workload identity.

        The returned runner identity must come from trusted runtime evidence such
        as workload OIDC, mTLS, or a runner attestation. It must never be copied
        from the signed payload or supplied as a caller assertion.
        """


def _validate_authority(
    *,
    contract: ReleaseControlContract,
    operation: ReleaseOperation,
    authority: AuthorityEvidence,
    now: datetime,
    verifier: AuthorityVerifierPort,
) -> str:
    now = _utc(now, "trusted clock")
    if type(authority) is not AuthorityEvidence:
        raise AuthorityRejected("authority must use the typed evidence envelope")
    try:
        _literal(authority.schema_version, 1, "authority.schema_version")
        _bounded_string(authority.purpose, "authority.purpose", maximum=64)
        _bounded_string(authority.audience, "authority.audience", maximum=64)
        _bounded_string(authority.repository, "authority.repository", maximum=129)
        if not isinstance(authority.target, TargetName):
            raise SchemaError("authority.target must be a TargetName")
        _digest(authority.contract_digest, "authority.contract_digest")
        _safe_id(authority.contract_edition, "authority.contract_edition")
        _digest(
            authority.required_checks_digest,
            "authority.required_checks_digest",
        )
        _canonical_uuid(authority.authority_id, "authority.authority_id")
        _safe_id(authority.release_id, "authority.release_id")
        _digest(authority.operation_fingerprint, "authority.operation_fingerprint")
        _safe_id(authority.approver_identity, "authority.approver_identity")
        _safe_id(authority.runner_identity, "authority.runner_identity")
        issued = _utc(authority.issued_at, "authority.issued_at")
        expires = _utc(authority.expires_at, "authority.expires_at")
        _bounded_int(authority.max_uses, "authority.max_uses", minimum=1, maximum=1)
        _safe_id(authority.nonce, "authority.nonce")
        _safe_id(authority.key_id, "authority.key_id")
        _bounded_string(authority.signature, "authority.signature", maximum=512)
    except SchemaError as exc:
        raise AuthorityRejected(str(exc)) from exc
    expected = {
        "schema_version": 1,
        "purpose": AUTHORITY_PURPOSE,
        "audience": AUTHORITY_AUDIENCE,
        "repository": contract.repository,
        "target": operation.target,
        "contract_digest": contract.contract_digest,
        "contract_edition": contract.contract_edition,
        "required_checks_digest": contract.required_checks_digest,
        "release_id": operation.release_id,
        "operation_fingerprint": operation.operation_fingerprint,
        "max_uses": 1,
    }
    for field, value in expected.items():
        if getattr(authority, field) != value:
            raise AuthorityRejected(f"authority scope mismatch: {field}")
    if issued > now or now >= expires:
        raise AuthorityRejected("authority is not currently valid")
    lifetime = (expires - issued).total_seconds()
    if not 0 < lifetime <= contract.authority_ttl_seconds:
        raise AuthorityRejected("authority lifetime exceeds the contract")
    try:
        verification = verifier.verify(
            key_id=authority.key_id,
            signed_payload=canonical_json_bytes(authority.signed_payload()),
            signature=authority.signature,
        )
    except Exception as exc:
        LOGGER.warning("authority verifier failed closed (%s)", type(exc).__name__)
        raise AuthorityRejected("authority verification failed") from exc
    if type(verification) is not AuthorityVerificationReceipt:
        raise AuthorityRejected("authority verifier returned an untyped assertion")
    expected_verification = {
        "key_id": authority.key_id,
        "authority_id": authority.authority_id,
        "signer_identity": authority.approver_identity,
        "runner_identity": authority.runner_identity,
        "signed_payload_digest": _hash(
            authority.signed_payload(),
            "authority signed payload",
        ),
        "trust_root_identity": contract.authority_trust_root_identity,
        "signature_valid": True,
        "independent_signer": True,
    }
    for field_name, expected_value in expected_verification.items():
        if getattr(verification, field_name) != expected_value:
            raise AuthorityRejected(f"authority verification mismatch: {field_name}")
    if authority.approver_identity == verification.runner_identity:
        raise AuthorityRejected("approver must be independent from the runner")
    return _hash(
        {
            "domain": "iwe.release-control.verified-authority-evidence.v1",
            "authority_evidence_hash": authority.evidence_hash,
            "verification_evidence_hash": verification.evidence_hash,
            "signed_payload_digest": verification.signed_payload_digest,
            "trust_root_identity": verification.trust_root_identity,
            "authenticated_runner_identity": verification.runner_identity,
        },
        "verified authority evidence",
    )


def _validate_operation_contract(
    operation: ReleaseOperation,
    contract: ReleaseControlContract,
    *,
    allow_production_draft: bool = False,
) -> None:
    if type(contract) is not ReleaseControlContract or not contract._validated:
        raise ContractError("contract was not produced by strict validation")
    if type(operation) is not ReleaseOperation or (operation._planned is not True):
        raise EvidenceRejected("operation was not produced by verified planning")
    if operation.repository != contract.repository:
        raise ContractError("operation repository differs from the contract")
    if operation.contract_digest != contract.contract_digest:
        raise ContractError(
            "operation contract digest differs from the loaded contract"
        )
    if operation.contract_edition != contract.contract_edition:
        raise ContractError(
            "operation contract edition differs from the loaded contract"
        )
    if operation.required_checks != contract.required_checks:
        raise ContractError("operation required checks differ from the loaded contract")
    if (
        operation.action is ReleaseAction.PROMOTE
        and operation.target is TargetName.PRODUCTION
        and (
            operation.pilot_qualification_receipt_hash is None
            or operation.qualified_pilot_applied_receipt_hash is None
        )
        and not allow_production_draft
    ):
        raise EvidenceRejected("production draft requires pilot qualification")


class ClaimDisposition(str, Enum):
    OWNER = "owner"
    DUPLICATE = "duplicate"


class OperationState(str, Enum):
    CLAIMED_LOCKED = "claimed_locked"
    OUTCOME_UNKNOWN = "outcome_unknown"
    APPLIED = "applied"
    OBSERVED_APPLIED = "observed_applied"
    OBSERVED_NOT_APPLIED = "observed_not_applied"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class LedgerClaimRequest:
    external_id: str
    authorization_id: str
    operation_fingerprint: str
    target_key: TargetName
    artifact_digest: str
    operation_kind: ReleaseAction
    contract_digest: str
    evidence_hash: str

    @classmethod
    def from_operation(
        cls,
        operation: ReleaseOperation,
        authority: AuthorityEvidence,
        verified_evidence_hash: str,
    ) -> LedgerClaimRequest:
        return cls(
            external_id=operation_external_id(operation.operation_fingerprint),
            authorization_id=authority.authority_id,
            operation_fingerprint=operation.operation_fingerprint,
            target_key=operation.target,
            artifact_digest=operation.artifact_digest,
            operation_kind=operation.action,
            contract_digest=operation.contract_digest,
            evidence_hash=_digest(
                verified_evidence_hash,
                "verified authority evidence",
            ),
        )


def operation_external_id(operation_fingerprint: str) -> str:
    fingerprint = _digest(operation_fingerprint, "operation_fingerprint")
    return f"rc1-{fingerprint.removeprefix('sha256:')}"


@dataclass(frozen=True)
class ClaimReceipt:
    disposition: ClaimDisposition
    external_id: str
    authorization_id: str
    operation_fingerprint: str
    target_key: TargetName
    artifact_digest: str
    operation_kind: ReleaseAction
    contract_digest: str
    evidence_hash: str
    state: OperationState
    state_version: int
    fencing_token: int
    durable: bool
    authority_use_burned: bool
    claim_id: str


@dataclass(frozen=True)
class NegativeMatrixQuery:
    evidence_ref: str
    repository: str
    contract_digest: str
    contract_edition: str
    pilot_source_ref: str
    production_source_ref: str


@dataclass(frozen=True)
class NegativeMatrixReceipt:
    """Proves the old branch-based auto-deploy is off on both environments."""

    evidence_ref: str
    repository: str
    contract_digest: str
    contract_edition: str
    pilot_source_ref: str
    production_source_ref: str
    verified_at: datetime
    approver_identity: str
    evidence_hash: str


@dataclass(frozen=True)
class PilotReceiptQuery:
    repository: str
    manifest_release_id: str
    operation_fingerprint: str
    artifact_digest: str
    embedded_manifest_hash: str
    contract_digest: str
    contract_edition: str
    required_checks_digest: str


@dataclass(frozen=True)
class AppliedReleaseReceipt:
    receipt_id: str
    claim_id: str
    repository: str
    release_id: str
    manifest_release_id: str
    operation_fingerprint: str
    action: ReleaseAction
    target: TargetName
    artifact_digest: str
    embedded_manifest_hash: str
    contract_digest: str
    contract_edition: str
    required_checks_digest: str
    state: OperationState
    state_version: int
    fencing_token: int
    durable: bool
    effect_confirmed_at: datetime
    provider_evidence_hash: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "receipt_id": self.receipt_id,
            "claim_id": self.claim_id,
            "repository": self.repository,
            "release_id": self.release_id,
            "manifest_release_id": self.manifest_release_id,
            "operation_fingerprint": self.operation_fingerprint,
            "action": self.action.value,
            "target": self.target.value,
            "artifact_digest": self.artifact_digest,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "required_checks_digest": self.required_checks_digest,
            "state": self.state.value,
            "state_version": self.state_version,
            "fencing_token": self.fencing_token,
            "durable": self.durable,
            "effect_confirmed_at": _timestamp(self.effect_confirmed_at),
            "provider_evidence_hash": self.provider_evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "applied release receipt")


@dataclass(frozen=True)
class PilotQualificationRequest:
    repository: str
    contract_digest: str
    contract_edition: str
    release_id: str
    pilot_operation_fingerprint: str
    pilot_claim_id: str
    pilot_applied_receipt_hash: str
    pilot_effect_confirmed_at: datetime
    artifact_digest: str
    image_reference: str
    embedded_manifest_hash: str
    target_profile_digest: str
    target_snapshot_hash: str
    target_config_digest: str
    required_signals: tuple[str, ...]
    excluded_verifier_identities: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "release_id": self.release_id,
            "pilot_operation_fingerprint": self.pilot_operation_fingerprint,
            "pilot_claim_id": self.pilot_claim_id,
            "pilot_applied_receipt_hash": self.pilot_applied_receipt_hash,
            "pilot_effect_confirmed_at": _timestamp(self.pilot_effect_confirmed_at),
            "artifact_digest": self.artifact_digest,
            "image_reference": self.image_reference,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "target_profile_digest": self.target_profile_digest,
            "target_snapshot_hash": self.target_snapshot_hash,
            "target_config_digest": self.target_config_digest,
            "required_signals": list(self.required_signals),
            "excluded_verifier_identities": list(self.excluded_verifier_identities),
        }

    @property
    def request_digest(self) -> str:
        return _hash(self.to_mapping(), "pilot qualification request")


@dataclass(frozen=True)
class PilotSignalResult:
    signal_id: str
    conclusion: str
    observed_at: datetime
    provider_reference_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _safe_id(self.signal_id, "pilot_signal.signal_id")
        _safe_id(self.conclusion, "pilot_signal.conclusion")
        _utc(self.observed_at, "pilot_signal.observed_at")
        _digest(
            self.provider_reference_hash,
            "pilot_signal.provider_reference_hash",
        )
        _digest(self.evidence_hash, "pilot_signal.evidence_hash")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "conclusion": self.conclusion,
            "observed_at": _timestamp(self.observed_at),
            "provider_reference_hash": self.provider_reference_hash,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True)
class PilotQualificationReceipt:
    schema_version: int
    receipt_id: str
    request_digest: str
    repository: str
    contract_digest: str
    contract_edition: str
    release_id: str
    pilot_operation_fingerprint: str
    pilot_claim_id: str
    pilot_applied_receipt_hash: str
    pilot_effect_confirmed_at: datetime
    artifact_digest: str
    image_reference: str
    embedded_manifest_hash: str
    target_profile_digest: str
    target_snapshot_hash: str
    target_config_digest: str
    required_signals: tuple[str, ...]
    excluded_verifier_identities: tuple[str, ...]
    signal_results: tuple[PilotSignalResult, ...]
    complete: bool
    independent_verifier: bool
    verifier_identity: str
    verified_at: datetime
    external_receipt_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "pilot_qualification.schema_version")
        _canonical_uuid(self.receipt_id, "pilot_qualification.receipt_id")
        _digest(self.request_digest, "pilot_qualification.request_digest")
        _bounded_string(
            self.repository,
            "pilot_qualification.repository",
            maximum=129,
        )
        _digest(self.contract_digest, "pilot_qualification.contract_digest")
        _safe_id(self.contract_edition, "pilot_qualification.contract_edition")
        _safe_id(self.release_id, "pilot_qualification.release_id")
        _canonical_uuid(self.pilot_claim_id, "pilot_qualification.pilot_claim_id")
        _utc(
            self.pilot_effect_confirmed_at,
            "pilot_qualification.pilot_effect_confirmed_at",
        )
        for field_name in (
            "pilot_operation_fingerprint",
            "pilot_applied_receipt_hash",
            "artifact_digest",
            "embedded_manifest_hash",
            "target_profile_digest",
            "target_snapshot_hash",
            "target_config_digest",
            "external_receipt_hash",
            "evidence_hash",
        ):
            _digest(getattr(self, field_name), f"pilot_qualification.{field_name}")
        validate_immutable_image_reference(self.image_reference, self.artifact_digest)
        _check_ids(self.required_signals, "pilot_qualification.required_signals")
        if self.excluded_verifier_identities != tuple(
            sorted(set(self.excluded_verifier_identities))
        ):
            raise SchemaError(
                "pilot qualification excluded identities must be sorted and unique"
            )
        for identity in self.excluded_verifier_identities:
            _safe_id(identity, "pilot_qualification.excluded_verifier_identities[]")
        if not isinstance(self.signal_results, tuple) or not self.signal_results:
            raise SchemaError("pilot qualification results must be a non-empty tuple")
        if any(type(result) is not PilotSignalResult for result in self.signal_results):
            raise SchemaError("pilot qualification contains an untyped signal")
        _boolean(self.complete, "pilot_qualification.complete")
        _boolean(
            self.independent_verifier,
            "pilot_qualification.independent_verifier",
        )
        _safe_id(self.verifier_identity, "pilot_qualification.verifier_identity")
        _utc(self.verified_at, "pilot_qualification.verified_at")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "request_digest": self.request_digest,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "release_id": self.release_id,
            "pilot_operation_fingerprint": self.pilot_operation_fingerprint,
            "pilot_claim_id": self.pilot_claim_id,
            "pilot_applied_receipt_hash": self.pilot_applied_receipt_hash,
            "pilot_effect_confirmed_at": _timestamp(self.pilot_effect_confirmed_at),
            "artifact_digest": self.artifact_digest,
            "image_reference": self.image_reference,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "target_profile_digest": self.target_profile_digest,
            "target_snapshot_hash": self.target_snapshot_hash,
            "target_config_digest": self.target_config_digest,
            "required_signals": list(self.required_signals),
            "excluded_verifier_identities": list(self.excluded_verifier_identities),
            "signal_results": [result.to_mapping() for result in self.signal_results],
            "complete": self.complete,
            "independent_verifier": self.independent_verifier,
            "verifier_identity": self.verifier_identity,
            "verified_at": _timestamp(self.verified_at),
            "external_receipt_hash": self.external_receipt_hash,
            "evidence_hash": self.evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "pilot qualification receipt")


class VerifiedPilotQualificationResolverPort(Protocol):
    def resolve_verified(
        self,
        request: PilotQualificationRequest,
    ) -> PilotQualificationReceipt:
        """Verify post-pilot runtime, readiness, canary, and data signals."""


@dataclass(frozen=True)
class QualificationRecordRequest:
    repository: str
    contract_digest: str
    contract_edition: str
    release_id: str
    pilot_claim_id: str
    pilot_operation_fingerprint: str
    pilot_applied_receipt_hash: str
    qualification_receipt_hash: str
    external_receipt_hash: str
    artifact_digest: str
    embedded_manifest_hash: str
    verifier_identity: str
    qualification_verified_at: datetime

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "release_id": self.release_id,
            "pilot_claim_id": self.pilot_claim_id,
            "pilot_operation_fingerprint": self.pilot_operation_fingerprint,
            "pilot_applied_receipt_hash": self.pilot_applied_receipt_hash,
            "qualification_receipt_hash": self.qualification_receipt_hash,
            "external_receipt_hash": self.external_receipt_hash,
            "artifact_digest": self.artifact_digest,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "verifier_identity": self.verifier_identity,
            "qualification_verified_at": _timestamp(self.qualification_verified_at),
        }

    @property
    def request_digest(self) -> str:
        return _hash(self.to_mapping(), "qualification record request")


@dataclass(frozen=True)
class DurableQualificationReceipt:
    schema_version: int
    record_id: str
    request_digest: str
    repository: str
    contract_digest: str
    contract_edition: str
    release_id: str
    pilot_claim_id: str
    pilot_operation_fingerprint: str
    pilot_applied_receipt_hash: str
    qualification_receipt_hash: str
    external_receipt_hash: str
    artifact_digest: str
    embedded_manifest_hash: str
    verifier_identity: str
    qualification_verified_at: datetime
    recorded_at: datetime
    state_version: int
    durable: bool
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "durable_qualification.schema_version")
        _canonical_uuid(self.record_id, "durable_qualification.record_id")
        _digest(self.request_digest, "durable_qualification.request_digest")
        _bounded_string(
            self.repository,
            "durable_qualification.repository",
            maximum=129,
        )
        _digest(self.contract_digest, "durable_qualification.contract_digest")
        _safe_id(
            self.contract_edition,
            "durable_qualification.contract_edition",
        )
        _safe_id(self.release_id, "durable_qualification.release_id")
        _safe_id(
            self.verifier_identity,
            "durable_qualification.verifier_identity",
        )
        _canonical_uuid(self.pilot_claim_id, "durable_qualification.pilot_claim_id")
        for field_name in (
            "pilot_operation_fingerprint",
            "pilot_applied_receipt_hash",
            "qualification_receipt_hash",
            "external_receipt_hash",
            "artifact_digest",
            "embedded_manifest_hash",
            "evidence_hash",
        ):
            _digest(getattr(self, field_name), f"durable_qualification.{field_name}")
        _utc(
            self.qualification_verified_at,
            "durable_qualification.qualification_verified_at",
        )
        _utc(self.recorded_at, "durable_qualification.recorded_at")
        _bounded_int(
            self.state_version,
            "durable_qualification.state_version",
            minimum=1,
            maximum=2**63 - 1,
        )
        _boolean(self.durable, "durable_qualification.durable")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "request_digest": self.request_digest,
            "repository": self.repository,
            "contract_digest": self.contract_digest,
            "contract_edition": self.contract_edition,
            "release_id": self.release_id,
            "pilot_claim_id": self.pilot_claim_id,
            "pilot_operation_fingerprint": self.pilot_operation_fingerprint,
            "pilot_applied_receipt_hash": self.pilot_applied_receipt_hash,
            "qualification_receipt_hash": self.qualification_receipt_hash,
            "external_receipt_hash": self.external_receipt_hash,
            "artifact_digest": self.artifact_digest,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "verifier_identity": self.verifier_identity,
            "qualification_verified_at": _timestamp(self.qualification_verified_at),
            "recorded_at": _timestamp(self.recorded_at),
            "state_version": self.state_version,
            "durable": self.durable,
            "evidence_hash": self.evidence_hash,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "durable qualification receipt")


class QualificationEvidencePort(Protocol):
    def record_durable(
        self,
        request: QualificationRecordRequest,
    ) -> DurableQualificationReceipt:
        """Append and return an external durable qualification receipt."""


@dataclass(frozen=True)
class ProviderMutationRequest:
    operation: ReleaseOperation
    image_reference: str
    expected_current_artifact_digest: str
    expected_target_config_digest: str
    expected_current_schema: int
    expected_migration_history_digest: str
    target_profile_digest: str
    target_snapshot_hash: str
    external_id: str
    claim_id: str
    fencing_token: int
    idempotency_key: str
    not_before: datetime
    not_after: datetime

    def __post_init__(self) -> None:
        if type(self.operation) is not ReleaseOperation or not self.operation._planned:
            raise EvidenceRejected("provider request requires a verified operation")
        if self.external_id != operation_external_id(
            self.operation.operation_fingerprint
        ):
            raise LedgerSafetyError("provider request external_id mismatch")
        if self.image_reference != self.operation.image_reference:
            raise EvidenceRejected("provider request image reference mismatch")
        validate_immutable_image_reference(
            self.image_reference,
            self.operation.artifact_digest,
        )
        expected_operation_fields = {
            "expected_current_artifact_digest": (
                self.operation.expected_current_digest
            ),
            "expected_target_config_digest": self.operation.target_config_digest,
            "expected_current_schema": self.operation.current_schema,
            "expected_migration_history_digest": (
                self.operation.migration_history_digest
            ),
            "target_profile_digest": self.operation.target_profile_digest,
            "target_snapshot_hash": self.operation.target_snapshot_hash,
        }
        for field_name, expected_value in expected_operation_fields.items():
            if getattr(self, field_name) != expected_value:
                raise EvidenceRejected(f"provider request mismatch: {field_name}")
        _canonical_uuid(self.claim_id, "provider_request.claim_id")
        _bounded_int(
            self.fencing_token,
            "provider_request.fencing_token",
            minimum=1,
            maximum=2**63 - 1,
        )
        expected_key = provider_idempotency_key(self.operation.operation_fingerprint)
        if self.idempotency_key != expected_key:
            raise LedgerSafetyError(
                "provider idempotency key was not derived from operation"
            )
        not_before = _utc(self.not_before, "provider_request.not_before")
        deadline = _utc(self.not_after, "provider_request.not_after")
        if not_before >= deadline:
            raise EvidenceRejected("provider mutation window is empty")
        if deadline > _utc(
            self.operation.evidence_valid_until,
            "operation.evidence_valid_until",
        ):
            raise EvidenceRejected("provider deadline exceeds planning evidence")

    def intent_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "domain": "iwe.release-control.provider-intent.v1",
            "operation_fingerprint": self.operation.operation_fingerprint,
            "external_id": self.external_id,
            "claim_id": self.claim_id,
            "idempotency_key": self.idempotency_key,
            "fencing_token": self.fencing_token,
            "not_before": _timestamp(self.not_before),
            "not_after": _timestamp(self.not_after),
            "image_reference": self.image_reference,
            "expected_current_artifact_digest": (self.expected_current_artifact_digest),
            "expected_target_config_digest": self.expected_target_config_digest,
            "expected_current_schema": self.expected_current_schema,
            "expected_migration_history_digest": (
                self.expected_migration_history_digest
            ),
            "target_profile_digest": self.target_profile_digest,
            "target_snapshot_hash": self.target_snapshot_hash,
        }

    @property
    def intent_hash(self) -> str:
        return _hash(self.intent_payload(), "provider mutation intent")


@dataclass(frozen=True)
class VerifiedProviderEffect:
    """Exact provider-side mutation record shared by apply and reconciliation."""

    operation_fingerprint: str
    external_id: str
    claim_id: str
    idempotency_key: str
    fencing_token: int
    not_before: datetime
    not_after: datetime
    applied_at: datetime
    observed_previous_artifact_digest: str
    observed_target_config_digest: str
    observed_current_schema: int
    observed_migration_history_digest: str
    artifact_digest: str
    image_reference: str
    target_profile_digest: str
    target_snapshot_hash: str
    provider_reference_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _digest(self.operation_fingerprint, "provider_result.operation_fingerprint")
        _safe_id(self.external_id, "provider_result.external_id")
        _canonical_uuid(self.claim_id, "provider_result.claim_id")
        _digest(self.idempotency_key, "provider_result.idempotency_key")
        _bounded_int(
            self.fencing_token,
            "provider_result.fencing_token",
            minimum=1,
            maximum=2**63 - 1,
        )
        _utc(self.not_before, "provider_result.not_before")
        _utc(self.not_after, "provider_result.not_after")
        _utc(self.applied_at, "provider_result.applied_at")
        for field_name in (
            "observed_previous_artifact_digest",
            "observed_target_config_digest",
            "observed_migration_history_digest",
            "artifact_digest",
            "target_profile_digest",
            "target_snapshot_hash",
            "provider_reference_hash",
            "evidence_hash",
        ):
            _digest(getattr(self, field_name), f"provider_effect.{field_name}")
        _bounded_int(
            self.observed_current_schema,
            "provider_effect.observed_current_schema",
            minimum=0,
            maximum=2_147_483_647,
        )
        validate_immutable_image_reference(
            self.image_reference,
            self.artifact_digest,
        )

    def exactly_matches(self, request: ProviderMutationRequest) -> bool:
        return (
            self.operation_fingerprint == request.operation.operation_fingerprint
            and self.external_id == request.external_id
            and self.claim_id == request.claim_id
            and self.idempotency_key == request.idempotency_key
            and self.fencing_token == request.fencing_token
            and self.not_before == request.not_before
            and self.not_after == request.not_after
            and request.not_before <= self.applied_at < request.not_after
            and self.observed_previous_artifact_digest
            == request.expected_current_artifact_digest
            and self.observed_target_config_digest
            == request.expected_target_config_digest
            and self.observed_current_schema == request.expected_current_schema
            and self.observed_migration_history_digest
            == request.expected_migration_history_digest
            and self.artifact_digest == request.operation.artifact_digest
            and self.image_reference == request.image_reference
            and self.target_profile_digest == request.target_profile_digest
            and self.target_snapshot_hash == request.target_snapshot_hash
            and _SHA256_RE.fullmatch(self.provider_reference_hash) is not None
            and _SHA256_RE.fullmatch(self.evidence_hash) is not None
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operation_fingerprint": self.operation_fingerprint,
            "external_id": self.external_id,
            "claim_id": self.claim_id,
            "idempotency_key": self.idempotency_key,
            "fencing_token": self.fencing_token,
            "not_before": _timestamp(self.not_before),
            "not_after": _timestamp(self.not_after),
            "applied_at": _timestamp(self.applied_at),
            "observed_previous_artifact_digest": (
                self.observed_previous_artifact_digest
            ),
            "observed_target_config_digest": self.observed_target_config_digest,
            "observed_current_schema": self.observed_current_schema,
            "observed_migration_history_digest": (
                self.observed_migration_history_digest
            ),
            "artifact_digest": self.artifact_digest,
            "image_reference": self.image_reference,
            "target_profile_digest": self.target_profile_digest,
            "target_snapshot_hash": self.target_snapshot_hash,
            "provider_reference_hash": self.provider_reference_hash,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True)
class ProviderApplyResult(VerifiedProviderEffect):
    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_mapping(), "provider apply receipt")


@dataclass(frozen=True)
class ProviderIntentRecordRequest:
    external_id: str
    claim_id: str
    operation_fingerprint: str
    expected_state: OperationState
    expected_version: int
    fencing_token: int
    provider_intent_hash: str
    not_before: datetime
    not_after: datetime

    @classmethod
    def from_provider_request(
        cls,
        request: ProviderMutationRequest,
        claim: ClaimReceipt,
    ) -> ProviderIntentRecordRequest:
        return cls(
            external_id=request.external_id,
            claim_id=claim.claim_id,
            operation_fingerprint=request.operation.operation_fingerprint,
            expected_state=OperationState.CLAIMED_LOCKED,
            expected_version=claim.state_version,
            fencing_token=claim.fencing_token,
            provider_intent_hash=request.intent_hash,
            not_before=request.not_before,
            not_after=request.not_after,
        )

    def __post_init__(self) -> None:
        _safe_id(self.external_id, "provider_intent.external_id")
        _canonical_uuid(self.claim_id, "provider_intent.claim_id")
        _digest(self.operation_fingerprint, "provider_intent.operation_fingerprint")
        if self.expected_state is not OperationState.CLAIMED_LOCKED:
            raise SchemaError("provider intent must CAS claimed_locked state")
        _bounded_int(
            self.expected_version,
            "provider_intent.expected_version",
            minimum=1,
            maximum=2**63 - 1,
        )
        _bounded_int(
            self.fencing_token,
            "provider_intent.fencing_token",
            minimum=1,
            maximum=2**63 - 1,
        )
        _digest(self.provider_intent_hash, "provider_intent.hash")
        not_before = _utc(self.not_before, "provider_intent.not_before")
        not_after = _utc(self.not_after, "provider_intent.not_after")
        if not_before >= not_after:
            raise SchemaError("provider intent window is empty")


@dataclass(frozen=True)
class LedgerOutcomeRequest:
    external_id: str
    claim_id: str
    operation_fingerprint: str
    expected_state: OperationState
    expected_version: int
    fencing_token: int
    next_state: OperationState
    target: TargetName
    repository: str
    release_id: str
    manifest_release_id: str
    action: ReleaseAction
    artifact_digest: str
    embedded_manifest_hash: str
    contract_digest: str
    contract_edition: str
    required_checks_digest: str
    evidence_hash: str


@dataclass(frozen=True)
class LedgerStateSnapshot:
    external_id: str
    claim_id: str
    operation_fingerprint: str
    target: TargetName
    artifact_digest: str
    contract_digest: str
    state: OperationState
    state_version: int
    fencing_token: int
    uncertainty_started_at: datetime
    provider_intent_hash: str | None = None
    provider_not_before: datetime | None = None
    provider_not_after: datetime | None = None


class ReleaseLedgerPort(Protocol):
    def negative_matrix_receipt(
        self,
        query: NegativeMatrixQuery,
    ) -> NegativeMatrixReceipt | None:
        """Return the durable receipt proving old auto-deploy is off, or None."""

    def pilot_applied_receipt(
        self,
        query: PilotReceiptQuery,
    ) -> AppliedReleaseReceipt | None:
        """Return a durable exact pilot receipt, never a latest-by-label guess."""

    def claim_and_burn(self, request: LedgerClaimRequest) -> ClaimReceipt:
        """Atomically append receipt, burn max_uses=1, and acquire target lock."""

    def record_provider_intent(
        self,
        request: ProviderIntentRecordRequest,
    ) -> LedgerStateSnapshot | None:
        """Durably CAS the exact provider request before any mutation call."""

    def record_outcome(
        self,
        request: LedgerOutcomeRequest,
    ) -> LedgerStateSnapshot | None:
        """CAS one runner-visible outcome using state/version/fencing."""

    def state_snapshot(self, operation_fingerprint: str) -> LedgerStateSnapshot | None:
        """Return a versioned state snapshot for auditor reconciliation."""

    def reconcile_compare_and_swap(
        self,
        request: ReconciliationCASRequest,
    ) -> LedgerStateSnapshot | None:
        """Auditor-only atomic transition; None means CAS conflict."""


class ReleaseProviderPort(Protocol):
    def apply(self, request: ProviderMutationRequest) -> ProviderApplyResult:
        """Atomically enforce current/idempotency/fence/deadline; never retry."""


def provider_idempotency_key(operation_fingerprint: str) -> str:
    _digest(operation_fingerprint, "operation_fingerprint")
    return _hash(
        {
            "domain": PROVIDER_IDEMPOTENCY_DOMAIN,
            "operation_fingerprint": operation_fingerprint,
        },
        "provider idempotency key",
    )


def _negative_matrix_query(contract: ReleaseControlContract) -> NegativeMatrixQuery:
    return NegativeMatrixQuery(
        evidence_ref=_digest(
            contract.negative_matrix_evidence_ref,
            "contract.cutover.negative_matrix_evidence_ref",
        ),
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        pilot_source_ref=contract.pilot_source_ref,
        production_source_ref=contract.production_source_ref,
    )


def _validate_negative_matrix_receipt(
    receipt: NegativeMatrixReceipt | None,
    contract: ReleaseControlContract,
    now: datetime,
) -> None:
    if type(receipt) is not NegativeMatrixReceipt:
        raise LedgerSafetyError(
            "cutover requires a durable negative-matrix receipt proving the "
            "old branch-based auto-deploy is disabled on both environments"
        )
    expected = {
        "evidence_ref": contract.negative_matrix_evidence_ref,
        "repository": contract.repository,
        "contract_digest": contract.contract_digest,
        "contract_edition": contract.contract_edition,
        "pilot_source_ref": contract.pilot_source_ref,
        "production_source_ref": contract.production_source_ref,
    }
    for field, value in expected.items():
        if getattr(receipt, field) != value:
            raise LedgerSafetyError(f"negative-matrix receipt mismatch: {field}")
    _safe_id(receipt.approver_identity, "negative_matrix_receipt.approver_identity")
    _digest(receipt.evidence_hash, "negative_matrix_receipt.evidence_hash")
    if _age_seconds(
        receipt.verified_at,
        now,
        "negative_matrix_receipt.verified_at",
    ) > contract.negative_matrix_max_age_seconds:
        raise EvidenceRejected("negative-matrix receipt is stale")


def _validate_pilot_receipt(
    receipt: AppliedReleaseReceipt | None,
    operation: ReleaseOperation,
    now: datetime,
) -> None:
    if type(receipt) is not AppliedReleaseReceipt:
        raise LedgerSafetyError(
            "production requires a durable exact pilot APPLIED receipt"
        )
    if type(receipt.action) is not ReleaseAction:
        raise LedgerSafetyError("pilot APPLIED receipt has an untyped action")
    if type(receipt.target) is not TargetName:
        raise LedgerSafetyError("pilot APPLIED receipt has an untyped target")
    if type(receipt.state) is not OperationState:
        raise LedgerSafetyError("pilot APPLIED receipt has an untyped state")
    if receipt.durable is not True:
        raise LedgerSafetyError("pilot APPLIED receipt is not durable")
    if receipt.state not in (
        OperationState.APPLIED,
        OperationState.OBSERVED_APPLIED,
    ):
        raise LedgerSafetyError("pilot receipt does not prove an applied effect")
    query = _pilot_receipt_query(operation)
    expected = {
        "repository": operation.repository,
        "release_id": operation.manifest_release_id,
        "manifest_release_id": operation.manifest_release_id,
        "operation_fingerprint": (operation.required_pilot_operation_fingerprint),
        "action": ReleaseAction.PROMOTE,
        "target": TargetName.PILOT,
        "artifact_digest": operation.artifact_digest,
        "embedded_manifest_hash": operation.embedded_manifest_hash,
        "contract_digest": query.contract_digest,
        "contract_edition": query.contract_edition,
        "required_checks_digest": query.required_checks_digest,
        "durable": True,
    }
    for field, value in expected.items():
        if getattr(receipt, field) != value:
            raise LedgerSafetyError(f"pilot APPLIED receipt mismatch: {field}")
    _canonical_uuid(receipt.receipt_id, "pilot_receipt.receipt_id")
    _canonical_uuid(receipt.claim_id, "pilot_receipt.claim_id")
    if (
        type(receipt.state_version) is not int
        or not 2 <= receipt.state_version <= 2**63 - 1
    ):
        raise LedgerSafetyError("pilot receipt state version is impossible")
    _age_seconds(
        receipt.effect_confirmed_at,
        now,
        "pilot_receipt.effect_confirmed_at",
    )
    _bounded_int(
        receipt.fencing_token,
        "pilot_receipt.fencing_token",
        minimum=1,
        maximum=2**63 - 1,
    )
    _digest(receipt.provider_evidence_hash, "pilot_receipt.provider_evidence_hash")


def _pilot_receipt_query(operation: ReleaseOperation) -> PilotReceiptQuery:
    if operation.action is ReleaseAction.ROLLBACK:
        contract_digest = operation.rollback_historical_contract_digest
        contract_edition = operation.rollback_historical_contract_edition
        required_checks_digest = operation.rollback_historical_required_checks_digest
    else:
        contract_digest = operation.contract_digest
        contract_edition = operation.contract_edition
        required_checks_digest = operation.required_checks_digest
    return PilotReceiptQuery(
        repository=operation.repository,
        manifest_release_id=operation.manifest_release_id,
        operation_fingerprint=operation.required_pilot_operation_fingerprint,
        artifact_digest=operation.artifact_digest,
        embedded_manifest_hash=operation.embedded_manifest_hash,
        contract_digest=_digest(contract_digest, "pilot query contract digest"),
        contract_edition=_safe_id(
            contract_edition,
            "pilot query contract edition",
        ),
        required_checks_digest=_digest(
            required_checks_digest,
            "pilot query required checks digest",
        ),
    )


def _validate_pilot_qualification(
    *,
    contract: ReleaseControlContract,
    request: PilotQualificationRequest,
    receipt: PilotQualificationReceipt,
    now: datetime,
) -> None:
    expected = {
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
        "complete": True,
        "independent_verifier": True,
    }
    for field_name, expected_value in expected.items():
        if getattr(receipt, field_name) != expected_value:
            raise EvidenceRejected(f"pilot qualification mismatch: {field_name}")
    if receipt.verifier_identity in request.excluded_verifier_identities:
        raise EvidenceRejected("pilot qualification verifier is not independent")
    signal_ids = tuple(result.signal_id for result in receipt.signal_results)
    if signal_ids != contract.pilot_qualification_required_signals:
        raise EvidenceRejected("pilot qualification signals are not exact")
    if any(result.conclusion != "success" for result in receipt.signal_results):
        raise EvidenceRejected("pilot qualification contains a failed signal")
    if len(
        {result.provider_reference_hash for result in receipt.signal_results}
    ) != len(receipt.signal_results):
        raise EvidenceRejected("pilot qualification reuses a signal reference")
    if len({result.evidence_hash for result in receipt.signal_results}) != len(
        receipt.signal_results
    ):
        raise EvidenceRejected("pilot qualification reuses signal evidence")
    verified_at = _utc(receipt.verified_at, "pilot_qualification.verified_at")
    if _age_seconds(verified_at, now, "pilot_qualification.verified_at") > (
        contract.pilot_qualification_max_age_seconds
    ):
        raise EvidenceRejected("pilot qualification receipt is stale")
    if any(
        not (
            _utc(request.pilot_effect_confirmed_at, "pilot effect")
            <= _utc(result.observed_at, "pilot_signal.observed_at")
            <= verified_at
        )
        for result in receipt.signal_results
    ):
        raise EvidenceRejected("pilot signal is not post-effect qualification evidence")
    if any(
        _age_seconds(result.observed_at, now, "pilot_signal.observed_at")
        > contract.pilot_qualification_max_age_seconds
        for result in receipt.signal_results
    ):
        raise EvidenceRejected("pilot qualification contains a stale signal")


def _pilot_qualification_valid_until(
    receipt: PilotQualificationReceipt,
    *,
    max_age_seconds: int,
) -> datetime:
    """Return the earliest expiry of the wrapper and every pilot signal."""

    oldest_evidence = min(
        _utc(receipt.verified_at, "pilot_qualification.verified_at"),
        *(
            _utc(result.observed_at, "pilot_signal.observed_at")
            for result in receipt.signal_results
        ),
    )
    return oldest_evidence + timedelta(seconds=max_age_seconds)


def _validate_durable_qualification(
    *,
    request: QualificationRecordRequest,
    receipt: DurableQualificationReceipt,
    now: datetime,
) -> None:
    expected = {
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
        "durable": True,
    }
    for field_name, expected_value in expected.items():
        if getattr(receipt, field_name) != expected_value:
            raise LedgerSafetyError(f"durable qualification mismatch: {field_name}")
    recorded_at = _utc(receipt.recorded_at, "durable_qualification.recorded_at")
    if recorded_at < _utc(request.qualification_verified_at, "qualification time"):
        raise LedgerSafetyError("qualification was recorded before verification")
    try:
        _age_seconds(recorded_at, now, "durable_qualification.recorded_at")
    except EvidenceRejected as exc:
        raise LedgerSafetyError(str(exc)) from exc


def finalize_production_operation(
    *,
    contract: ReleaseControlContract,
    plan: PromotionPlan,
    ledger: ReleaseLedgerPort,
    qualification_resolver: VerifiedPilotQualificationResolverPort,
    qualification_evidence: QualificationEvidencePort,
    target_resolver: VerifiedTargetStateResolverPort,
    clock: TrustedClockPort,
) -> ReleaseOperation:
    """Qualify pilot, persist proof, then resolve a fresh production target."""

    if (
        type(plan) is not PromotionPlan
        or not plan._validated
        or type(plan.operations) is not tuple
        or len(plan.operations) != 2
    ):
        raise EvidenceRejected("production finalization requires a promotion plan")
    pilot, draft = plan.operations
    _validate_operation_contract(pilot, contract)
    _validate_operation_contract(draft, contract, allow_production_draft=True)
    manifest = _validate_manifest(plan._manifest, contract)
    if (
        type(plan._artifact) is not ArtifactDescriptor
        or type(plan._check_suite) is not CheckSuiteReceipt
        or type(plan._source_candidate) is not SourceCandidateReceipt
        or type(plan._pilot_target) is not TargetSnapshot
        or type(plan._production_target) is not TargetSnapshot
    ):
        raise EvidenceRejected("promotion plan contains untyped sealed evidence")
    shared_expected = {
        "repository": contract.repository,
        "contract_digest": contract.contract_digest,
        "contract_edition": contract.contract_edition,
        "required_checks": contract.required_checks,
        "release_id": manifest.release_id,
        "manifest_release_id": manifest.release_id,
        "artifact_digest": plan._artifact.digest,
        "image_reference": plan._artifact.image_reference,
        "embedded_manifest_hash": manifest.manifest_hash,
        "artifact_receipt_hash": plan._artifact.resolution_receipt_hash,
        "artifact_resolver_identity": plan._artifact.resolver_identity,
        "check_suite_receipt_hash": plan._check_suite.receipt_hash,
        "source_candidate_receipt_hash": plan._source_candidate.receipt_hash,
        "migration_class": manifest.migration_class,
        "schema_min": manifest.schema_min,
        "schema_max": manifest.schema_max,
    }
    if (
        plan.release_id != manifest.release_id
        or plan.artifact_digest != plan._artifact.digest
        or plan.embedded_manifest_hash != manifest.manifest_hash
        or plan.contract_digest != contract.contract_digest
        or plan.contract_edition != contract.contract_edition
        or plan.required_checks != contract.required_checks
        or any(
            getattr(operation, field_name) != expected_value
            for operation in (pilot, draft)
            for field_name, expected_value in shared_expected.items()
        )
    ):
        raise EvidenceRejected("promotion plan sealed evidence linkage is invalid")
    for operation, target in (
        (pilot, plan._pilot_target),
        (draft, plan._production_target),
    ):
        target_expected = {
            "target": target.target,
            "target_profile_digest": target.target_profile_digest,
            "target_snapshot_hash": target.receipt_hash,
            "target_source_identity": target.source_identity,
            "expected_current_digest": target.expected_current_digest,
            "target_config_digest": target.target_config_digest,
            "migration_history_digest": target.migration_history_digest,
            "current_schema": target.current_schema,
        }
        if any(
            getattr(operation, field_name) != expected_value
            for field_name, expected_value in target_expected.items()
        ):
            raise EvidenceRejected("promotion plan target evidence linkage is invalid")
    if (
        pilot.action is not ReleaseAction.PROMOTE
        or pilot.target is not TargetName.PILOT
        or draft.action is not ReleaseAction.PROMOTE
        or draft.target is not TargetName.PRODUCTION
        or draft.required_pilot_operation_fingerprint != pilot.operation_fingerprint
        or draft.pilot_qualification_receipt_hash is not None
        or draft.qualified_pilot_applied_receipt_hash is not None
    ):
        raise EvidenceRejected("promotion plan pilot/production linkage is invalid")
    clock = _MonotonicTrustedClock(clock)
    pilot_read_started_at = _trusted_now(clock)
    try:
        pilot_receipt = ledger.pilot_applied_receipt(_pilot_receipt_query(draft))
    except Exception as exc:
        LOGGER.warning("pilot receipt read failed closed (%s)", type(exc).__name__)
        raise LedgerSafetyError(
            "cannot verify pilot receipt for qualification"
        ) from exc
    pilot_read_at = _trusted_post_call_now(
        clock,
        pilot_read_started_at,
        "pilot receipt read",
    )
    _validate_pilot_receipt(pilot_receipt, draft, pilot_read_at)
    qualification_request = PilotQualificationRequest(
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        release_id=draft.manifest_release_id,
        pilot_operation_fingerprint=pilot.operation_fingerprint,
        pilot_claim_id=pilot_receipt.claim_id,
        pilot_applied_receipt_hash=pilot_receipt.receipt_hash,
        pilot_effect_confirmed_at=pilot_receipt.effect_confirmed_at,
        artifact_digest=pilot.artifact_digest,
        image_reference=pilot.image_reference,
        embedded_manifest_hash=pilot.embedded_manifest_hash,
        target_profile_digest=pilot.target_profile_digest,
        target_snapshot_hash=pilot.target_snapshot_hash,
        target_config_digest=pilot.target_config_digest,
        required_signals=contract.pilot_qualification_required_signals,
        excluded_verifier_identities=tuple(
            sorted(
                {
                    plan._check_suite.verifier_identity,
                    plan._artifact.resolver_identity,
                    plan._source_candidate.reviewer_identity,
                    plan._source_candidate.verifier_identity,
                    plan._pilot_target.source_identity,
                    plan._production_target.source_identity,
                }
            )
        ),
    )
    qualification_started_at = _trusted_now(clock)
    try:
        qualification = qualification_resolver.resolve_verified(qualification_request)
    except Exception as exc:
        LOGGER.warning(
            "pilot qualification resolver failed closed (%s)",
            type(exc).__name__,
        )
        raise EvidenceRejected("verified pilot qualification failed") from exc
    qualification_resolved_at = _trusted_post_call_now(
        clock,
        qualification_started_at,
        "pilot qualification",
    )
    if type(qualification) is not PilotQualificationReceipt:
        raise EvidenceRejected("pilot qualification returned an untyped assertion")
    _validate_pilot_qualification(
        contract=contract,
        request=qualification_request,
        receipt=qualification,
        now=qualification_resolved_at,
    )
    record_request = QualificationRecordRequest(
        repository=contract.repository,
        contract_digest=contract.contract_digest,
        contract_edition=contract.contract_edition,
        release_id=draft.manifest_release_id,
        pilot_claim_id=pilot_receipt.claim_id,
        pilot_operation_fingerprint=pilot.operation_fingerprint,
        pilot_applied_receipt_hash=pilot_receipt.receipt_hash,
        qualification_receipt_hash=qualification.receipt_hash,
        external_receipt_hash=qualification.external_receipt_hash,
        artifact_digest=pilot.artifact_digest,
        embedded_manifest_hash=pilot.embedded_manifest_hash,
        verifier_identity=qualification.verifier_identity,
        qualification_verified_at=qualification.verified_at,
    )
    record_started_at = _trusted_now(clock)
    try:
        durable_qualification = qualification_evidence.record_durable(record_request)
    except Exception as exc:
        LOGGER.warning("qualification record failed closed (%s)", type(exc).__name__)
        raise LedgerSafetyError("pilot qualification was not durably recorded") from exc
    post_record_now = _trusted_post_call_now(
        clock,
        record_started_at,
        "qualification recording",
    )
    if type(durable_qualification) is not DurableQualificationReceipt:
        raise LedgerSafetyError("qualification store returned an untyped receipt")
    _validate_pilot_qualification(
        contract=contract,
        request=qualification_request,
        receipt=qualification,
        now=post_record_now,
    )
    _validate_durable_qualification(
        request=record_request,
        receipt=durable_qualification,
        now=post_record_now,
    )
    production_target = _resolve_target_snapshot(
        contract=contract,
        operation_release_id=plan._manifest.release_id,
        manifest=plan._manifest,
        artifact=plan._artifact,
        target=TargetName.PRODUCTION,
        resolver=target_resolver,
        clock=clock,
    )
    if production_target.source_identity == qualification.verifier_identity:
        raise EvidenceRejected(
            "fresh production target source is not independent from qualification"
        )
    if production_target.source_identity != draft.target_source_identity:
        raise EvidenceRejected(
            "fresh production target resolver identity differs from the planned resolver"
        )
    final_now = _trusted_now(clock)
    if final_now < post_record_now:
        raise EvidenceRejected("trusted clock moved backwards during finalization")
    _validate_pilot_qualification(
        contract=contract,
        request=qualification_request,
        receipt=qualification,
        now=final_now,
    )
    _validate_durable_qualification(
        request=record_request,
        receipt=durable_qualification,
        now=final_now,
    )
    if (
        _age_seconds(
            production_target.captured_at,
            final_now,
            "production target snapshot",
        )
        > MAX_TARGET_SNAPSHOT_AGE_SECONDS
    ):
        raise EvidenceRejected("production target snapshot expired during finalization")
    evidence_valid_until = min(
        _check_suite_valid_until(
            plan._check_suite,
            max_age_seconds=contract.check_suite_max_age_seconds,
        ),
        _utc(plan._source_candidate.verified_at, "source_candidate.verified_at")
        + timedelta(seconds=contract.source_candidate_max_age_seconds),
        _pilot_qualification_valid_until(
            qualification,
            max_age_seconds=contract.pilot_qualification_max_age_seconds,
        ),
    )
    final_deadline = min(
        evidence_valid_until,
        _utc(plan._artifact.resolved_at, "artifact resolved_at")
        + timedelta(seconds=MAX_ARTIFACT_RECEIPT_AGE_SECONDS),
        _utc(production_target.captured_at, "production target captured_at")
        + timedelta(seconds=MAX_TARGET_SNAPSHOT_AGE_SECONDS),
    )
    if final_now >= final_deadline:
        raise EvidenceRejected("production evidence expired during finalization")
    qualification_gate_hash = _hash(
        {
            "domain": "iwe.release-control.qualified-production-gate.v1",
            "pilot_applied_receipt_hash": pilot_receipt.receipt_hash,
            "qualification_receipt_hash": qualification.receipt_hash,
            "durable_qualification_receipt_hash": (durable_qualification.receipt_hash),
        },
        "qualified production gate",
    )
    return _operation_from_verified_evidence(
        contract=contract,
        operation_release_id=plan._manifest.release_id,
        action=ReleaseAction.PROMOTE,
        manifest=plan._manifest,
        artifact=plan._artifact,
        check_suite=plan._check_suite,
        source_candidate=plan._source_candidate,
        pilot_qualification_receipt_hash=durable_qualification.receipt_hash,
        qualified_pilot_applied_receipt_hash=pilot_receipt.receipt_hash,
        target=production_target,
        evidence_valid_until=evidence_valid_until,
        additional_gate_evidence_hash=qualification_gate_hash,
        required_pilot_operation_fingerprint=pilot.operation_fingerprint,
    )


def _validate_claim(receipt: ClaimReceipt, request: LedgerClaimRequest) -> None:
    if type(receipt) is not ClaimReceipt:
        raise LedgerSafetyError("ledger returned an untyped claim")
    if type(receipt.target_key) is not TargetName:
        raise LedgerSafetyError("ledger claim has an untyped target")
    if type(receipt.operation_kind) is not ReleaseAction:
        raise LedgerSafetyError("ledger claim has an untyped operation kind")
    for field in (
        "external_id",
        "operation_fingerprint",
        "target_key",
        "artifact_digest",
        "operation_kind",
        "contract_digest",
    ):
        if getattr(receipt, field) != getattr(request, field):
            raise LedgerSafetyError(f"ledger claim mismatch: {field}")
    _canonical_uuid(receipt.authorization_id, "claim.authorization_id")
    _digest(receipt.evidence_hash, "claim.evidence_hash")
    if receipt.durable is not True or receipt.authority_use_burned is not True:
        raise LedgerSafetyError("ledger did not durably claim and burn authority")
    _canonical_uuid(receipt.claim_id, "claim.claim_id")
    minimum_state_version = 1 if receipt.state is OperationState.CLAIMED_LOCKED else 2
    _bounded_int(
        receipt.state_version,
        "claim.state_version",
        minimum=minimum_state_version,
        maximum=2**63 - 1,
    )
    _bounded_int(
        receipt.fencing_token,
        "claim.fencing_token",
        minimum=1,
        maximum=2**63 - 1,
    )
    if receipt.disposition is ClaimDisposition.OWNER:
        if receipt.authorization_id != request.authorization_id:
            raise LedgerSafetyError("ledger claim mismatch: authorization_id")
        if receipt.evidence_hash != request.evidence_hash:
            raise LedgerSafetyError("ledger claim mismatch: evidence_hash")
        if receipt.state is not OperationState.CLAIMED_LOCKED:
            raise LedgerSafetyError("owner claim is not claimed_locked")
    elif receipt.disposition is ClaimDisposition.DUPLICATE:
        if type(receipt.state) is not OperationState:
            raise LedgerSafetyError("duplicate claim has an unknown state")
    else:
        raise LedgerSafetyError("ledger claim disposition is unsupported")


def _outcome_request(
    *,
    claim: ClaimReceipt,
    operation: ReleaseOperation,
    next_state: OperationState,
    evidence_hash: str,
) -> LedgerOutcomeRequest:
    return LedgerOutcomeRequest(
        external_id=claim.external_id,
        claim_id=claim.claim_id,
        operation_fingerprint=operation.operation_fingerprint,
        expected_state=OperationState.CLAIMED_LOCKED,
        expected_version=claim.state_version,
        fencing_token=claim.fencing_token,
        next_state=next_state,
        target=operation.target,
        repository=operation.repository,
        release_id=operation.release_id,
        manifest_release_id=operation.manifest_release_id,
        action=operation.action,
        artifact_digest=operation.artifact_digest,
        embedded_manifest_hash=operation.embedded_manifest_hash,
        contract_digest=operation.contract_digest,
        contract_edition=operation.contract_edition,
        required_checks_digest=operation.required_checks_digest,
        evidence_hash=_digest(evidence_hash, "outcome.evidence_hash"),
    )


class ExecutionState(str, Enum):
    APPLIED = "applied"
    OUTCOME_UNKNOWN = "outcome_unknown"
    OBSERVED_NOT_APPLIED = "observed_not_applied"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class ExecutionResult:
    state: ExecutionState
    operation_fingerprint: str
    provider_called: bool


def _execution_state_from_durable_state(state: OperationState) -> ExecutionState:
    """Return the already-recorded effect instead of a generic duplicate."""

    if state in (OperationState.APPLIED, OperationState.OBSERVED_APPLIED):
        return ExecutionState.APPLIED
    if state is OperationState.OBSERVED_NOT_APPLIED:
        return ExecutionState.OBSERVED_NOT_APPLIED
    if state is OperationState.MANUAL_REVIEW:
        return ExecutionState.MANUAL_REVIEW
    return ExecutionState.OUTCOME_UNKNOWN


def _existing_durable_result(
    *,
    ledger: ReleaseLedgerPort,
    operation: ReleaseOperation,
) -> ExecutionResult | None:
    """Return an exact prior outcome without reopening transient mutation gates."""

    try:
        raw_snapshot = ledger.state_snapshot(operation.operation_fingerprint)
    except Exception as exc:
        LOGGER.warning("ledger state read failed closed (%s)", type(exc).__name__)
        raise LedgerSafetyError("cannot read existing ledger state") from exc
    if raw_snapshot is None:
        return None
    snapshot = _validate_ledger_snapshot(raw_snapshot, operation)
    return ExecutionResult(
        state=_execution_state_from_durable_state(snapshot.state),
        operation_fingerprint=operation.operation_fingerprint,
        provider_called=False,
    )


def _validate_owner_claim_snapshot(
    *,
    ledger: ReleaseLedgerPort,
    operation: ReleaseOperation,
    claim: ClaimReceipt,
) -> None:
    """Prove that the owner receipt exactly matches the durable claimed row."""

    try:
        raw_snapshot = ledger.state_snapshot(operation.operation_fingerprint)
    except Exception as exc:
        LOGGER.warning("owner state read failed closed (%s)", type(exc).__name__)
        raise LedgerSafetyError("cannot verify owner ledger state") from exc
    snapshot = _validate_ledger_snapshot(raw_snapshot, operation)
    expected = {
        "external_id": claim.external_id,
        "claim_id": claim.claim_id,
        "state": OperationState.CLAIMED_LOCKED,
        "state_version": claim.state_version,
        "fencing_token": claim.fencing_token,
    }
    for field, value in expected.items():
        if getattr(snapshot, field) != value:
            raise LedgerSafetyError(f"owner ledger state mismatch: {field}")


def _validate_duplicate_claim_snapshot(
    *,
    ledger: ReleaseLedgerPort,
    operation: ReleaseOperation,
    claim: ClaimReceipt,
) -> LedgerStateSnapshot:
    """Return an exact, monotonically reachable state for a duplicate claim."""

    try:
        raw_snapshot = ledger.state_snapshot(operation.operation_fingerprint)
    except Exception as exc:
        LOGGER.warning("duplicate state read failed closed (%s)", type(exc).__name__)
        raise LedgerSafetyError("cannot verify duplicate ledger state") from exc
    snapshot = _validate_ledger_snapshot(raw_snapshot, operation)
    expected = {
        "external_id": claim.external_id,
        "claim_id": claim.claim_id,
        "fencing_token": claim.fencing_token,
    }
    for field, value in expected.items():
        if getattr(snapshot, field) != value:
            raise LedgerSafetyError(f"duplicate ledger state mismatch: {field}")
    version_delta = snapshot.state_version - claim.state_version
    if version_delta == 0:
        reachable = snapshot.state is claim.state
    elif claim.state is OperationState.CLAIMED_LOCKED:
        reachable_by_delta = {
            1: {
                OperationState.CLAIMED_LOCKED,
                OperationState.OUTCOME_UNKNOWN,
                OperationState.APPLIED,
            },
            2: {
                OperationState.OUTCOME_UNKNOWN,
                OperationState.APPLIED,
                OperationState.OBSERVED_APPLIED,
                OperationState.OBSERVED_NOT_APPLIED,
                OperationState.MANUAL_REVIEW,
            },
            3: {
                OperationState.OBSERVED_APPLIED,
                OperationState.OBSERVED_NOT_APPLIED,
                OperationState.MANUAL_REVIEW,
            },
        }
        reachable = snapshot.state in reachable_by_delta.get(version_delta, set())
    elif claim.state is OperationState.OUTCOME_UNKNOWN and version_delta == 1:
        reachable = snapshot.state in {
            OperationState.OBSERVED_APPLIED,
            OperationState.OBSERVED_NOT_APPLIED,
            OperationState.MANUAL_REVIEW,
        }
    else:
        reachable = False
    if not reachable:
        raise LedgerSafetyError("duplicate ledger state is not monotonically reachable")
    return snapshot


def _record_uncertain(
    *,
    ledger: ReleaseLedgerPort,
    claim: ClaimReceipt,
    operation: ReleaseOperation,
    reason_code: str,
) -> None:
    evidence_hash = _hash(
        {"reason_code": _safe_id(reason_code, "reason_code")},
        "uncertain outcome",
    )
    try:
        ledger.record_outcome(
            _outcome_request(
                claim=claim,
                operation=operation,
                next_state=OperationState.OUTCOME_UNKNOWN,
                evidence_hash=evidence_hash,
            )
        )
    except Exception as exc:  # noqa: BLE001 - ledger port failure is fail-closed
        LOGGER.warning("ledger uncertainty CAS failed closed (%s)", type(exc).__name__)


def _temporal_gate_after_claim(
    *,
    clock: TrustedClockPort,
    initial_now: datetime,
    authority: AuthorityEvidence,
    claim: ClaimReceipt,
    operation: ReleaseOperation,
    ledger: ReleaseLedgerPort,
) -> datetime | None:
    """Recheck trusted time after slow ports and before provider mutation."""

    try:
        current_now = _trusted_now(clock)
    except EvidenceRejected:
        _record_uncertain(
            ledger=ledger,
            claim=claim,
            operation=operation,
            reason_code="pre-provider-clock-unavailable",
        )
        return None
    planning_deadline = _utc(
        operation.evidence_valid_until,
        "operation.evidence_valid_until",
    )
    authority_deadline = _utc(authority.expires_at, "authority.expires_at")
    if (
        current_now < initial_now
        or current_now >= planning_deadline
        or current_now >= authority_deadline
    ):
        _record_uncertain(
            ledger=ledger,
            claim=claim,
            operation=operation,
            reason_code="pre-provider-evidence-expired",
        )
        return None
    return current_now


def _validate_provider_intent_snapshot(
    *,
    snapshot: LedgerStateSnapshot | None,
    request: ProviderIntentRecordRequest,
    operation: ReleaseOperation,
) -> LedgerStateSnapshot:
    """Require an exact durable CAS of provider intent before mutation."""

    validated = _validate_ledger_snapshot(snapshot, operation)
    expected = {
        "external_id": request.external_id,
        "claim_id": request.claim_id,
        "operation_fingerprint": request.operation_fingerprint,
        "state": OperationState.CLAIMED_LOCKED,
        "state_version": request.expected_version + 1,
        "fencing_token": request.fencing_token,
        "provider_intent_hash": request.provider_intent_hash,
        "provider_not_before": request.not_before,
        "provider_not_after": request.not_after,
    }
    for field_name, expected_value in expected.items():
        if getattr(validated, field_name) != expected_value:
            raise LedgerSafetyError(f"provider intent mismatch: {field_name}")
    return validated


def execute_operation(
    *,
    contract: ReleaseControlContract,
    operation: ReleaseOperation,
    authority: AuthorityEvidence,
    clock: TrustedClockPort,
    authority_verifier: AuthorityVerifierPort,
    ledger: ReleaseLedgerPort,
    provider: ReleaseProviderPort,
) -> ExecutionResult:
    """Execute at most once after all contract, pilot, authority, and CAS gates."""

    _validate_operation_contract(operation, contract)
    existing_result = _existing_durable_result(ledger=ledger, operation=operation)
    if existing_result is not None:
        return existing_result
    if not contract.cutover_enabled:
        raise CutoverBlocked("cutover is disabled by the repository contract")
    clock = _MonotonicTrustedClock(clock)
    now = _trusted_now(clock)
    negative_matrix_query = _negative_matrix_query(contract)
    negative_matrix_read_started_at = now
    try:
        negative_matrix_receipt = ledger.negative_matrix_receipt(
            negative_matrix_query
        )
    except Exception as exc:
        LOGGER.warning(
            "negative-matrix receipt read failed closed (%s)", type(exc).__name__
        )
        raise LedgerSafetyError(
            "cannot verify old auto-deploy is disabled"
        ) from exc
    now = _trusted_post_call_now(
        clock,
        negative_matrix_read_started_at,
        "negative-matrix receipt read before execution",
    )
    _validate_negative_matrix_receipt(negative_matrix_receipt, contract, now)
    if now >= _utc(operation.evidence_valid_until, "operation.evidence_valid_until"):
        raise EvidenceRejected("verified planning evidence has expired")
    if operation.target is TargetName.PRODUCTION:
        query = _pilot_receipt_query(operation)
        pilot_read_started_at = now
        try:
            pilot_receipt = ledger.pilot_applied_receipt(query)
        except Exception as exc:
            LOGGER.warning("pilot receipt read failed closed (%s)", type(exc).__name__)
            raise LedgerSafetyError("cannot verify pilot APPLIED receipt") from exc
        now = _trusted_post_call_now(
            clock,
            pilot_read_started_at,
            "pilot receipt read before execution",
        )
        _validate_pilot_receipt(pilot_receipt, operation, now)
        if (
            operation.qualified_pilot_applied_receipt_hash is not None
            and pilot_receipt.receipt_hash
            != operation.qualified_pilot_applied_receipt_hash
        ):
            raise LedgerSafetyError(
                "pilot APPLIED receipt differs from the qualified receipt"
            )
    verified_authority_evidence_hash = _validate_authority(
        contract=contract,
        operation=operation,
        authority=authority,
        now=now,
        verifier=authority_verifier,
    )
    claim_request = LedgerClaimRequest.from_operation(
        operation,
        authority,
        verified_authority_evidence_hash,
    )
    try:
        claim = ledger.claim_and_burn(claim_request)
    except Exception as exc:
        LOGGER.warning("ledger claim failed closed (%s)", type(exc).__name__)
        raise LedgerSafetyError("durable claim failed before provider call") from exc
    _validate_claim(claim, claim_request)
    if claim.disposition is ClaimDisposition.OWNER:
        _validate_owner_claim_snapshot(
            ledger=ledger,
            operation=operation,
            claim=claim,
        )
    else:
        duplicate_snapshot = _validate_duplicate_claim_snapshot(
            ledger=ledger,
            operation=operation,
            claim=claim,
        )
        return ExecutionResult(
            state=_execution_state_from_durable_state(duplicate_snapshot.state),
            operation_fingerprint=operation.operation_fingerprint,
            provider_called=False,
        )

    provider_not_before = _temporal_gate_after_claim(
        clock=clock,
        initial_now=now,
        authority=authority,
        claim=claim,
        operation=operation,
        ledger=ledger,
    )
    if provider_not_before is None:
        return ExecutionResult(
            state=ExecutionState.OUTCOME_UNKNOWN,
            operation_fingerprint=operation.operation_fingerprint,
            provider_called=False,
        )
    provider_request = ProviderMutationRequest(
        operation=operation,
        image_reference=operation.image_reference,
        expected_current_artifact_digest=operation.expected_current_digest,
        expected_target_config_digest=operation.target_config_digest,
        expected_current_schema=operation.current_schema,
        expected_migration_history_digest=operation.migration_history_digest,
        target_profile_digest=operation.target_profile_digest,
        target_snapshot_hash=operation.target_snapshot_hash,
        external_id=claim.external_id,
        claim_id=claim.claim_id,
        fencing_token=claim.fencing_token,
        idempotency_key=provider_idempotency_key(operation.operation_fingerprint),
        not_before=provider_not_before,
        not_after=min(
            _utc(operation.evidence_valid_until, "operation.evidence_valid_until"),
            _utc(authority.expires_at, "authority.expires_at"),
        ),
    )
    intent_request = ProviderIntentRecordRequest.from_provider_request(
        provider_request,
        claim,
    )
    try:
        raw_intent_snapshot = ledger.record_provider_intent(intent_request)
        intent_snapshot = _validate_provider_intent_snapshot(
            snapshot=raw_intent_snapshot,
            request=intent_request,
            operation=operation,
        )
    except Exception as exc:  # noqa: BLE001 - ledger reply is fail-closed
        LOGGER.warning("provider intent CAS failed closed (%s)", type(exc).__name__)
        _record_uncertain(
            ledger=ledger,
            claim=claim,
            operation=operation,
            reason_code="provider-intent-unavailable",
        )
        return ExecutionResult(
            state=ExecutionState.OUTCOME_UNKNOWN,
            operation_fingerprint=operation.operation_fingerprint,
            provider_called=False,
        )
    claim = replace(claim, state_version=intent_snapshot.state_version)
    if (
        _temporal_gate_after_claim(
            clock=clock,
            initial_now=provider_not_before,
            authority=authority,
            claim=claim,
            operation=operation,
            ledger=ledger,
        )
        is None
    ):
        return ExecutionResult(
            state=ExecutionState.OUTCOME_UNKNOWN,
            operation_fingerprint=operation.operation_fingerprint,
            provider_called=False,
        )
    try:
        provider_result = provider.apply(provider_request)
    except Exception as exc:  # noqa: BLE001 - provider outcome is intentionally unknown
        LOGGER.warning("provider call outcome is unknown (%s)", type(exc).__name__)
        _record_uncertain(
            ledger=ledger,
            claim=claim,
            operation=operation,
            reason_code="provider-exception",
        )
        return ExecutionResult(
            state=ExecutionState.OUTCOME_UNKNOWN,
            operation_fingerprint=operation.operation_fingerprint,
            provider_called=True,
        )
    if type(
        provider_result
    ) is not ProviderApplyResult or not provider_result.exactly_matches(
        provider_request
    ):
        _record_uncertain(
            ledger=ledger,
            claim=claim,
            operation=operation,
            reason_code="provider-response-mismatch",
        )
        return ExecutionResult(
            state=ExecutionState.OUTCOME_UNKNOWN,
            operation_fingerprint=operation.operation_fingerprint,
            provider_called=True,
        )
    outcome = _outcome_request(
        claim=claim,
        operation=operation,
        next_state=OperationState.APPLIED,
        evidence_hash=provider_result.receipt_hash,
    )
    try:
        applied = ledger.record_outcome(outcome)
    except Exception as exc:  # noqa: BLE001 - ledger port failure is fail-closed
        LOGGER.warning("applied receipt CAS failed closed (%s)", type(exc).__name__)
        applied = None
    applied_is_exact = type(applied) is LedgerStateSnapshot and (
        applied.external_id == claim.external_id
        and applied.claim_id == claim.claim_id
        and applied.operation_fingerprint == operation.operation_fingerprint
        and applied.target is operation.target
        and applied.artifact_digest == operation.artifact_digest
        and applied.contract_digest == operation.contract_digest
        and applied.state is OperationState.APPLIED
        and applied.state_version == claim.state_version + 1
        and applied.fencing_token == claim.fencing_token
        and applied.provider_intent_hash == intent_snapshot.provider_intent_hash
        and applied.provider_not_before == intent_snapshot.provider_not_before
        and applied.provider_not_after == intent_snapshot.provider_not_after
    )
    if not applied_is_exact:
        return ExecutionResult(
            state=ExecutionState.OUTCOME_UNKNOWN,
            operation_fingerprint=operation.operation_fingerprint,
            provider_called=True,
        )
    return ExecutionResult(
        state=ExecutionState.APPLIED,
        operation_fingerprint=operation.operation_fingerprint,
        provider_called=True,
    )


@dataclass(frozen=True)
class ProviderObservation(VerifiedProviderEffect):
    """Read-only view of the same immutable provider effect used by apply."""

    def exactly_matches_state(
        self,
        operation: ReleaseOperation,
        state: LedgerStateSnapshot,
    ) -> bool:
        if (
            state.provider_intent_hash is None
            or state.provider_not_before is None
            or state.provider_not_after is None
        ):
            return False
        try:
            request = ProviderMutationRequest(
                operation=operation,
                image_reference=operation.image_reference,
                expected_current_artifact_digest=operation.expected_current_digest,
                expected_target_config_digest=operation.target_config_digest,
                expected_current_schema=operation.current_schema,
                expected_migration_history_digest=operation.migration_history_digest,
                target_profile_digest=operation.target_profile_digest,
                target_snapshot_hash=operation.target_snapshot_hash,
                external_id=state.external_id,
                claim_id=state.claim_id,
                fencing_token=state.fencing_token,
                idempotency_key=provider_idempotency_key(
                    operation.operation_fingerprint
                ),
                not_before=state.provider_not_before,
                not_after=state.provider_not_after,
            )
        except ReleaseControlError:
            return False
        return (
            request.intent_hash == state.provider_intent_hash
            and self.exactly_matches(request)
        )


@dataclass(frozen=True)
class ObservationEnvelope:
    schema_version: int
    snapshot_id: str
    source_identity: str
    operation_fingerprint: str
    target: TargetName
    target_profile_digest: str
    complete: bool
    captured_at: datetime
    settled_through: datetime
    observations: tuple[ProviderObservation, ...]
    evidence_hash: str

    def __post_init__(self) -> None:
        _literal(self.schema_version, 1, "observation_envelope.schema_version")
        _canonical_uuid(self.snapshot_id, "observation_envelope.snapshot_id")
        _safe_id(self.source_identity, "observation_envelope.source_identity")
        _digest(
            self.operation_fingerprint, "observation_envelope.operation_fingerprint"
        )
        if not isinstance(self.target, TargetName):
            raise SchemaError("observation_envelope.target must be a TargetName")
        _digest(
            self.target_profile_digest,
            "observation_envelope.target_profile_digest",
        )
        _boolean(self.complete, "observation_envelope.complete")
        _utc(self.captured_at, "observation_envelope.captured_at")
        _utc(self.settled_through, "observation_envelope.settled_through")
        if not isinstance(self.observations, tuple) or len(self.observations) > 100:
            raise SchemaError(
                "observation_envelope.observations must be a bounded tuple"
            )
        if any(type(item) is not ProviderObservation for item in self.observations):
            raise SchemaError("observation_envelope contains an untyped observation")
        _digest(self.evidence_hash, "observation_envelope.evidence_hash")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_identity": self.source_identity,
            "operation_fingerprint": self.operation_fingerprint,
            "target": self.target.value,
            "target_profile_digest": self.target_profile_digest,
            "complete": self.complete,
            "captured_at": _timestamp(self.captured_at),
            "settled_through": _timestamp(self.settled_through),
            "observations": [item.to_mapping() for item in self.observations],
            "evidence_hash": self.evidence_hash,
        }


class ProviderObserverPort(Protocol):
    def observe_verified(
        self,
        operation: ReleaseOperation,
        uncertainty_started_at: datetime,
    ) -> Sequence[ObservationEnvelope]:
        """Return complete, authenticated, read-only provider snapshots."""


@dataclass(frozen=True)
class ReconciliationCASRequest:
    external_id: str
    operation_fingerprint: str
    expected_state: OperationState
    expected_version: int
    fencing_token: int
    next_state: OperationState
    evidence_hash: str
    observation_reference_hashes: tuple[str, ...]


@dataclass(frozen=True)
class ReconciliationResult:
    state: OperationState
    resolved: bool
    exact_match_count: int
    snapshot_count: int


def _validate_ledger_snapshot(
    snapshot: LedgerStateSnapshot | None,
    operation: ReleaseOperation,
) -> LedgerStateSnapshot:
    if type(snapshot) is not LedgerStateSnapshot:
        raise LedgerSafetyError("uncertain operation has no versioned ledger snapshot")
    if type(snapshot.target) is not TargetName:
        raise LedgerSafetyError("ledger state has an untyped target")
    if type(snapshot.state) is not OperationState:
        raise LedgerSafetyError("ledger state has an untyped operation state")
    expected = {
        "external_id": operation_external_id(operation.operation_fingerprint),
        "operation_fingerprint": operation.operation_fingerprint,
        "target": operation.target,
        "artifact_digest": operation.artifact_digest,
        "contract_digest": operation.contract_digest,
    }
    for field, value in expected.items():
        if getattr(snapshot, field) != value:
            raise LedgerSafetyError(f"ledger state mismatch: {field}")
    _canonical_uuid(snapshot.claim_id, "ledger.claim_id")
    minimum_state_version = 1 if snapshot.state is OperationState.CLAIMED_LOCKED else 2
    _bounded_int(
        snapshot.state_version,
        "ledger.state_version",
        minimum=minimum_state_version,
        maximum=2**63 - 1,
    )
    _bounded_int(
        snapshot.fencing_token,
        "ledger.fencing_token",
        minimum=1,
        maximum=2**63 - 1,
    )
    uncertainty_started_at = _utc(
        snapshot.uncertainty_started_at,
        "ledger.uncertainty_started_at",
    )
    intent_values = (
        snapshot.provider_intent_hash,
        snapshot.provider_not_before,
        snapshot.provider_not_after,
    )
    if any(value is None for value in intent_values):
        if any(value is not None for value in intent_values):
            raise LedgerSafetyError("ledger provider intent is incomplete")
        if snapshot.state in (
            OperationState.APPLIED,
            OperationState.OBSERVED_APPLIED,
        ):
            raise LedgerSafetyError("applied ledger state has no provider intent")
    else:
        _digest(snapshot.provider_intent_hash, "ledger.provider_intent_hash")
        not_before = _utc(
            snapshot.provider_not_before,
            "ledger.provider_not_before",
        )
        not_after = _utc(snapshot.provider_not_after, "ledger.provider_not_after")
        if not_before < uncertainty_started_at or not_before >= not_after:
            raise LedgerSafetyError("ledger provider intent window is invalid")
    return snapshot


def _validate_observation_envelopes(
    *,
    operation: ReleaseOperation,
    state: LedgerStateSnapshot,
    envelopes: Sequence[ObservationEnvelope],
    now: datetime,
) -> tuple[tuple[ObservationEnvelope, ...], tuple[ProviderObservation, ...]]:
    if not 1 <= len(envelopes) <= 10:
        raise EvidenceRejected("reconciliation requires 1..10 bounded snapshots")
    typed = tuple(envelopes)
    if any(type(envelope) is not ObservationEnvelope for envelope in typed):
        raise EvidenceRejected("observer returned an untyped snapshot")
    if len({envelope.snapshot_id for envelope in typed}) != len(typed):
        raise EvidenceRejected("reconciliation snapshot identities are not unique")
    if len({envelope.evidence_hash for envelope in typed}) != len(typed):
        raise EvidenceRejected("reconciliation snapshot evidence is not unique")
    ordered = tuple(sorted(typed, key=lambda item: item.captured_at))
    if ordered != typed:
        raise EvidenceRejected("reconciliation snapshots are not chronological")
    minimum_settled = _utc(
        state.uncertainty_started_at,
        "ledger.uncertainty_started_at",
    ) + timedelta(seconds=RECONCILIATION_MIN_SETTLING_SECONDS)
    for envelope in typed:
        expected = {
            "operation_fingerprint": operation.operation_fingerprint,
            "target": operation.target,
            "target_profile_digest": operation.target_profile_digest,
            "complete": True,
        }
        for field, value in expected.items():
            if getattr(envelope, field) != value:
                raise EvidenceRejected(f"observation envelope mismatch: {field}")
        if _age_seconds(envelope.captured_at, now, "observation.captured_at") > (
            RECONCILIATION_MAX_SNAPSHOT_AGE_SECONDS
        ):
            raise EvidenceRejected("observation snapshot is stale")
        captured = _utc(envelope.captured_at, "observation.captured_at")
        settled = _utc(envelope.settled_through, "observation.settled_through")
        if settled > captured or settled < minimum_settled:
            raise EvidenceRejected(
                "observation snapshot has not crossed the settling horizon"
            )
        if any(
            _utc(item.applied_at, "provider_observation.applied_at") > captured
            for item in envelope.observations
        ):
            raise EvidenceRejected("provider effect occurred after its snapshot")
    observations = tuple(item for envelope in typed for item in envelope.observations)
    return typed, observations


def _reconciliation_decision(
    *,
    operation: ReleaseOperation,
    state: LedgerStateSnapshot,
    envelopes: tuple[ObservationEnvelope, ...],
    observations: tuple[ProviderObservation, ...],
) -> tuple[OperationState | None, tuple[str, ...], int]:
    contradictory = any(
        not item.exactly_matches_state(operation, state) for item in observations
    )
    exact = {
        item.provider_reference_hash: item
        for item in observations
        if item.exactly_matches_state(operation, state)
    }
    references = tuple(sorted(exact))
    if contradictory:
        return OperationState.MANUAL_REVIEW, references, len(exact)
    if len(exact) == 1:
        return OperationState.OBSERVED_APPLIED, references, 1
    if len(exact) > 1:
        return OperationState.MANUAL_REVIEW, references, len(exact)
    if len(envelopes) < RECONCILIATION_MIN_ZERO_SNAPSHOTS:
        return None, (), 0
    interval = (envelopes[-1].captured_at - envelopes[0].captured_at).total_seconds()
    if interval < RECONCILIATION_MIN_ZERO_INTERVAL_SECONDS:
        return None, (), 0
    mutation_horizon = _utc(
        operation.evidence_valid_until,
        "operation.evidence_valid_until",
    )
    if any(
        _utc(envelope.settled_through, "observation.settled_through") < mutation_horizon
        for envelope in envelopes
    ):
        return None, (), 0
    return OperationState.OBSERVED_NOT_APPLIED, (), 0


def reconcile_uncertain_operation(
    *,
    operation: ReleaseOperation,
    ledger: ReleaseLedgerPort,
    observer: ProviderObserverPort,
    clock: TrustedClockPort,
) -> ReconciliationResult:
    """Resolve exact evidence through one version/fencing compare-and-swap."""

    try:
        raw_state = ledger.state_snapshot(operation.operation_fingerprint)
    except Exception as exc:
        LOGGER.warning("ledger state read failed closed (%s)", type(exc).__name__)
        raise LedgerSafetyError("cannot read uncertain ledger state") from exc
    state = _validate_ledger_snapshot(raw_state, operation)
    if state.state not in (
        OperationState.CLAIMED_LOCKED,
        OperationState.OUTCOME_UNKNOWN,
    ):
        exact_match_count = (
            1
            if state.state in (OperationState.APPLIED, OperationState.OBSERVED_APPLIED)
            else 0
        )
        return ReconciliationResult(state.state, True, exact_match_count, 0)
    clock = _MonotonicTrustedClock(clock)
    initial_now = _trusted_now(clock)
    try:
        raw_envelopes = observer.observe_verified(
            operation,
            state.uncertainty_started_at,
        )
        now = _trusted_now(clock)
        if now < initial_now:
            raise EvidenceRejected(
                "trusted clock moved backwards during reconciliation"
            )
        envelopes, observations = _validate_observation_envelopes(
            operation=operation,
            state=state,
            envelopes=raw_envelopes,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - observer boundary is fail-closed
        LOGGER.warning("reconciliation evidence failed closed (%s)", type(exc).__name__)
        return ReconciliationResult(state.state, False, 0, 0)
    next_state, references, exact_count = _reconciliation_decision(
        operation=operation,
        state=state,
        envelopes=envelopes,
        observations=observations,
    )
    if next_state is None:
        return ReconciliationResult(state.state, False, 0, len(envelopes))
    envelope_hashes = [
        _hash(item.to_mapping(), "observation envelope") for item in envelopes
    ]
    evidence_hash = _hash(
        {
            "domain": "iwe.release-control.reconciliation.v1",
            "operation_fingerprint": operation.operation_fingerprint,
            "expected_state": state.state.value,
            "expected_version": state.state_version,
            "fencing_token": state.fencing_token,
            "next_state": next_state.value,
            "envelope_hashes": envelope_hashes,
            "observation_reference_hashes": list(references),
        },
        "reconciliation evidence",
    )
    request = ReconciliationCASRequest(
        external_id=state.external_id,
        operation_fingerprint=operation.operation_fingerprint,
        expected_state=state.state,
        expected_version=state.state_version,
        fencing_token=state.fencing_token,
        next_state=next_state,
        evidence_hash=evidence_hash,
        observation_reference_hashes=references,
    )
    try:
        updated = ledger.reconcile_compare_and_swap(request)
    except Exception as exc:  # noqa: BLE001 - ledger port failure is fail-closed
        LOGGER.warning("reconciliation CAS failed closed (%s)", type(exc).__name__)
        updated = None
    if type(updated) is not LedgerStateSnapshot:
        return ReconciliationResult(state.state, False, exact_count, len(envelopes))
    if (
        updated.external_id != state.external_id
        or updated.claim_id != state.claim_id
        or updated.operation_fingerprint != operation.operation_fingerprint
        or updated.target is not operation.target
        or updated.artifact_digest != operation.artifact_digest
        or updated.contract_digest != operation.contract_digest
        or updated.state is not next_state
        or updated.state_version != state.state_version + 1
        or updated.fencing_token != state.fencing_token
        or updated.provider_intent_hash != state.provider_intent_hash
        or updated.provider_not_before != state.provider_not_before
        or updated.provider_not_after != state.provider_not_after
    ):
        return ReconciliationResult(state.state, False, exact_count, len(envelopes))
    return ReconciliationResult(next_state, True, exact_count, len(envelopes))


reconcile_outcome_unknown = reconcile_uncertain_operation


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the WP-562 release contract")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-contract")
    validate.add_argument("contract", type=Path)
    validate.add_argument("--require-cutover-disabled", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        contract = load_release_control_contract(args.contract)
        if args.require_cutover_disabled and contract.cutover_enabled:
            raise ContractError("cutover must remain disabled in this phase")
    except ReleaseControlError as exc:
        LOGGER.error("release-control contract rejected: %s", exc)
        return 2
    print(
        json.dumps(
            {
                "status": "valid",
                "contract_digest": contract.contract_digest,
                "contract_edition": contract.contract_edition,
                "required_checks_digest": contract.required_checks_digest,
                "cutover_enabled": contract.cutover_enabled,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
