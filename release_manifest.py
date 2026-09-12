"""Strict, transport-neutral schema for the manifest embedded in a release.

This module is deliberately importable by both runtime code and build/control
tools.  It contains no provider, network, database, or deployment capability.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

MANIFEST_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RELEASE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")
_CHECK_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_MAX_CHECKS = 32
_MAX_SCHEMA_REVISION = 2_147_483_647


class ReleaseManifestError(ValueError):
    """Manifest bytes or values do not match the exact public schema."""


class MigrationClass(str, Enum):
    NONE = "none"
    EXPAND = "expand"
    CONTRACT = "contract"
    DATA_REWRITE = "data_rewrite"


def validate_migration_semantics(
    migration_class: Any,
    schema_min: Any,
    schema_max: Any,
    *,
    path: str = "migration",
) -> tuple[MigrationClass, int, int]:
    """Validate the one migration-class/range contract shared by every boundary."""

    try:
        normalized_class = MigrationClass(migration_class)
    except (TypeError, ValueError) as exc:
        raise ReleaseManifestError(f"{path}.migration_class is unsupported") from exc
    normalized_min = _exact_int(
        schema_min,
        f"{path}.schema_min",
        minimum=0,
        maximum=_MAX_SCHEMA_REVISION,
    )
    normalized_max = _exact_int(
        schema_max,
        f"{path}.schema_max",
        minimum=0,
        maximum=_MAX_SCHEMA_REVISION,
    )
    if normalized_min > normalized_max:
        raise ReleaseManifestError(f"{path} schema range is inverted")
    if normalized_class is MigrationClass.NONE:
        if normalized_min != 0 or normalized_max != 0:
            raise ReleaseManifestError(
                f"{path} no-migration declaration must use schema 0..0"
            )
    elif normalized_max == 0:
        raise ReleaseManifestError(
            f"{path} migration declaration must name a schema revision"
        )
    return normalized_class, normalized_min, normalized_max


def _reject_json_constant(value: str) -> None:
    raise ReleaseManifestError(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def loads_strict_json(payload: str | bytes) -> Any:
    """Parse UTF-8 JSON while rejecting duplicate keys and non-finite values."""

    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="strict")
        if not isinstance(payload, str):
            raise ReleaseManifestError("JSON payload must be text or bytes")
        return json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except ReleaseManifestError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError("invalid UTF-8 JSON payload") from exc


def canonical_json_bytes(value: Any) -> bytes:
    """Return the sole UTF-8 representation used for release-control hashes."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return encoded.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReleaseManifestError("value is not canonical-JSON encodable") from exc


def canonical_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _exact_object(value: Any, expected: set[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseManifestError(f"{path} must be an object")
    actual = set(value)
    if actual != expected:
        raise ReleaseManifestError(f"{path} has missing or extra fields")
    return value


def _exact_int(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReleaseManifestError(f"{path} is outside its integer range")
    return value


def _full_match(value: Any, pattern: re.Pattern[str], path: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ReleaseManifestError(f"{path} has an invalid identifier")
    return value


@dataclass(frozen=True)
class EmbeddedManifest:
    schema_version: int
    release_id: str
    source_commit: str
    source_tree: str
    build_contract_digest: str
    migration_class: MigrationClass
    schema_min: int
    schema_max: int
    required_checks: tuple[str, ...]

    @classmethod
    def from_mapping(cls, payload: Any) -> EmbeddedManifest:
        obj = _exact_object(
            payload,
            {
                "schema_version",
                "release_id",
                "source_commit",
                "source_tree",
                "build_contract_digest",
                "migration_class",
                "schema_min",
                "schema_max",
                "required_checks",
                "platform",
            },
            "embedded_manifest",
        )
        if (
            obj["schema_version"] != MANIFEST_SCHEMA_VERSION
            or type(obj["schema_version"]) is not int
        ):
            raise ReleaseManifestError("embedded_manifest.schema_version must equal 1")
        release_id = _full_match(
            obj["release_id"], _RELEASE_ID_RE, "embedded_manifest.release_id"
        )
        source_commit = _full_match(
            obj["source_commit"], _GIT_OID_RE, "embedded_manifest.source_commit"
        )
        source_tree = _full_match(
            obj["source_tree"], _GIT_OID_RE, "embedded_manifest.source_tree"
        )
        build_contract_digest = _full_match(
            obj["build_contract_digest"],
            _SHA256_RE,
            "embedded_manifest.build_contract_digest",
        )
        migration_class, schema_min, schema_max = validate_migration_semantics(
            obj["migration_class"],
            obj["schema_min"],
            obj["schema_max"],
            path="embedded_manifest",
        )

        raw_checks = obj["required_checks"]
        if not isinstance(raw_checks, list) or not 1 <= len(raw_checks) <= _MAX_CHECKS:
            raise ReleaseManifestError(
                "embedded_manifest.required_checks has invalid length"
            )
        required_checks = tuple(
            _full_match(item, _CHECK_ID_RE, "embedded_manifest.required_checks[]")
            for item in raw_checks
        )
        if required_checks != tuple(sorted(set(required_checks))):
            raise ReleaseManifestError(
                "embedded_manifest.required_checks must be sorted and unique"
            )

        platform = _exact_object(
            obj["platform"], {"os", "architecture"}, "embedded_manifest.platform"
        )
        if platform["os"] != "linux" or platform["architecture"] != "amd64":
            raise ReleaseManifestError("embedded_manifest platform must be linux/amd64")
        return cls(
            schema_version=MANIFEST_SCHEMA_VERSION,
            release_id=release_id,
            source_commit=source_commit,
            source_tree=source_tree,
            build_contract_digest=build_contract_digest,
            migration_class=migration_class,
            schema_min=schema_min,
            schema_max=schema_max,
            required_checks=required_checks,
        )

    def to_mapping(self) -> dict[str, Any]:
        """Return pre-build facts; OCI/self digests are intentionally absent."""

        return {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "build_contract_digest": self.build_contract_digest,
            "migration_class": self.migration_class.value,
            "schema_min": self.schema_min,
            "schema_max": self.schema_max,
            "required_checks": list(self.required_checks),
            "platform": {"os": "linux", "architecture": "amd64"},
        }

    @property
    def manifest_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True)
class ConfigurationReference:
    repository_commit: str
    path: str
    digest: str

    @classmethod
    def from_mapping(cls, payload: Any) -> ConfigurationReference:
        obj = _exact_object(
            payload, {"repository_commit", "path", "digest"}, "configuration_reference"
        )
        path = obj["path"]
        if (
            not isinstance(path, str)
            or len(path) > 512
            or re.fullmatch(r"[A-Za-z0-9._/-]+", path) is None
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ReleaseManifestError("configuration_reference.path is unsafe")
        return cls(
            repository_commit=_full_match(
                obj["repository_commit"], _GIT_OID_RE, "configuration_reference.commit"
            ),
            path=path,
            digest=_full_match(
                obj["digest"], _SHA256_RE, "configuration_reference.digest"
            ),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "repository_commit": self.repository_commit,
            "path": self.path,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ReleaseConfigurationManifest:
    """Expected release configuration, outside the image and not runtime proof.

    Strict parsing establishes structure only. A verified target resolver must
    check committed reference contents and actual configuration; the existing
    independent operation authority must authorize the resulting fingerprint.
    """

    schema_version: int
    release_id: str
    embedded_manifest_hash: str
    governance_repository: str
    pilot: ConfigurationReference
    production: ConfigurationReference
    environment_delta_registry: ConfigurationReference

    @classmethod
    def from_mapping(cls, payload: Any) -> ReleaseConfigurationManifest:
        obj = _exact_object(
            payload,
            {
                "schema_version",
                "release_id",
                "embedded_manifest_hash",
                "governance_repository",
                "configuration_profiles",
                "environment_delta_registry",
            },
            "release_configuration_manifest",
        )
        if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
            raise ReleaseManifestError("release configuration schema_version must be 1")
        repository = obj["governance_repository"]
        if (
            not isinstance(repository, str)
            or len(repository) > 129
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository
            )
            is None
        ):
            raise ReleaseManifestError("governance_repository must be an owner/repo")
        profiles = _exact_object(
            obj["configuration_profiles"],
            {"pilot", "production"},
            "configuration_profiles",
        )
        return cls(
            schema_version=1,
            release_id=_full_match(obj["release_id"], _RELEASE_ID_RE, "release_id"),
            embedded_manifest_hash=_full_match(
                obj["embedded_manifest_hash"], _SHA256_RE, "embedded_manifest_hash"
            ),
            governance_repository=repository,
            pilot=ConfigurationReference.from_mapping(profiles["pilot"]),
            production=ConfigurationReference.from_mapping(profiles["production"]),
            environment_delta_registry=ConfigurationReference.from_mapping(
                obj["environment_delta_registry"]
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "governance_repository": self.governance_repository,
            "configuration_profiles": {
                "pilot": self.pilot.to_mapping(),
                "production": self.production.to_mapping(),
            },
            "environment_delta_registry": self.environment_delta_registry.to_mapping(),
        }

    @property
    def manifest_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
