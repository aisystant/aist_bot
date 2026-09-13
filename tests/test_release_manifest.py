from __future__ import annotations

import pytest

from release_manifest import (
    EmbeddedManifest,
    ReleaseConfigurationManifest,
    ReleaseManifestError,
    canonical_sha256,
    loads_strict_json,
)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "release_id": "release-2026-09-03-01",
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "build_contract_digest": "sha256:" + "3" * 64,
        "migration_class": "expand",
        "schema_min": 4,
        "schema_max": 6,
        "required_checks": ["build", "test"],
        "platform": {"os": "linux", "architecture": "amd64"},
    }
    payload.update(overrides)
    return payload


def test_manifest_round_trip_has_one_canonical_hash() -> None:
    manifest = EmbeddedManifest.from_mapping(_payload())

    assert manifest.to_mapping() == _payload()
    assert manifest.manifest_hash == canonical_sha256(_payload())


@pytest.mark.parametrize("schema_version", [0, 2, 999, True])
def test_manifest_rejects_unknown_schema_versions(schema_version: object) -> None:
    with pytest.raises(ReleaseManifestError):
        EmbeddedManifest.from_mapping(_payload(schema_version=schema_version))


@pytest.mark.parametrize(
    "check_id",
    ["SECRET_TOKEN=value", "secret value", "../secret", "x\ud800"],
)
def test_manifest_rejects_unsafe_or_free_form_check_ids(check_id: str) -> None:
    with pytest.raises(ReleaseManifestError):
        EmbeddedManifest.from_mapping(_payload(required_checks=[check_id]))


def test_manifest_rejects_duplicate_json_keys() -> None:
    with pytest.raises(ReleaseManifestError):
        loads_strict_json('{"schema_version":1,"schema_version":1}')


def test_manifest_rejects_extra_self_digest() -> None:
    with pytest.raises(ReleaseManifestError):
        EmbeddedManifest.from_mapping(_payload(manifest_hash="sha256:" + "a" * 64))


@pytest.mark.parametrize(
    ("migration_class", "schema_min", "schema_max"),
    [
        ("none", 1, 1),
        ("none", 0, 1),
        ("expand", 0, 0),
        ("contract", 0, 0),
        ("data_rewrite", 0, 0),
    ],
)
def test_manifest_rejects_semantically_impossible_migration_ranges(
    migration_class: str,
    schema_min: int,
    schema_max: int,
) -> None:
    with pytest.raises(ReleaseManifestError, match="migration|no-migration"):
        EmbeddedManifest.from_mapping(
            _payload(
                migration_class=migration_class,
                schema_min=schema_min,
                schema_max=schema_max,
            )
        )


def _configuration_payload():
    embedded = EmbeddedManifest.from_mapping(_payload())

    def reference(path, character):
        return {
            "repository_commit": "1" * 40,
            "path": path,
            "digest": "sha256:" + character * 64,
        }

    return {
        "schema_version": 1,
        "release_id": embedded.release_id,
        "embedded_manifest_hash": embedded.manifest_hash,
        "governance_repository": "example/governance",
        "configuration_profiles": {
            "pilot": reference("profiles/pilot.json", "a"),
            "production": reference("profiles/production.json", "b"),
        },
        "environment_delta_registry": reference("profiles/deltas.json", "c"),
    }


def test_configuration_manifest_round_trip_is_outside_the_embedded_image() -> None:
    payload = _configuration_payload()
    embedded = EmbeddedManifest.from_mapping(_payload())
    configuration = ReleaseConfigurationManifest.from_mapping(payload)

    assert configuration.to_mapping() == payload
    assert configuration.manifest_hash == canonical_sha256(payload)
    assert configuration.embedded_manifest_hash == embedded.manifest_hash
    assert "configuration_profiles" not in embedded.to_mapping()


@pytest.mark.parametrize("field", list(_configuration_payload()))
def test_configuration_manifest_rejects_every_missing_field(field) -> None:
    payload = _configuration_payload()
    del payload[field]
    with pytest.raises(ReleaseManifestError):
        ReleaseConfigurationManifest.from_mapping(payload)


@pytest.mark.parametrize("target", ["pilot", "production"])
def test_configuration_manifest_requires_both_profiles(target) -> None:
    payload = _configuration_payload()
    del payload["configuration_profiles"][target]
    with pytest.raises(ReleaseManifestError):
        ReleaseConfigurationManifest.from_mapping(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("embedded_manifest_hash", "latest"),
        ("governance_repository", "https://token@example.test/repo"),
        ("governance_repository", "../repo"),
        ("runtime_artifact_verified", True),
        ("approved_by", "caller-assertion"),
    ],
)
def test_configuration_manifest_rejects_invalid_fields_and_authority_claims(
    field, value
) -> None:
    payload = _configuration_payload()
    payload[field] = value
    with pytest.raises(ReleaseManifestError):
        ReleaseConfigurationManifest.from_mapping(payload)


@pytest.mark.parametrize(
    "reference", ["pilot", "production", "environment_delta_registry"]
)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_commit", "main"),
        ("digest", "latest"),
        ("path", "../secrets"),
        ("path", "/absolute/path"),
        ("path", "profiles//pilot.json"),
        ("path", "profiles/./pilot.json"),
        ("path", "profiles\\pilot.json"),
        ("credential", "forbidden"),
    ],
)
def test_every_configuration_reference_is_immutable_and_bounded(
    reference, field, value
) -> None:
    payload = _configuration_payload()
    owner = (
        payload
        if reference == "environment_delta_registry"
        else payload["configuration_profiles"]
    )
    owner[reference][field] = value
    with pytest.raises(ReleaseManifestError):
        ReleaseConfigurationManifest.from_mapping(payload)
