#!/usr/bin/env python3
"""Observe P-18 Railway source configuration without authorizing a deployment.

Supply explicit project/environment/service UUIDs and either --input PATH (a
GraphQL response fixture, no network) or --live (one read-only CLI query).
Only selected source, identity, trigger and deployment-status fields are read.
The configured registry digest never counts as proof of the running image.

Exit 1: evidence available, release readiness still gated. Exit 2: evidence
unavailable. This observer cannot return release-ready: image auto updates,
runtime artifact identity and exclusive deployment authority need independent
evidence. It neither reads that evidence nor changes Railway configuration.

Schema checked with Railway CLI 5.37.4 introspection, 2026-09-12.
https://docs.railway.com/cli/api
https://docs.railway.com/deployments/image-auto-updates
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

MAX_INPUT_BYTES = 1024 * 1024
QUERY = """
query ObserveReleaseSource(
  $projectId: String!, $environmentId: String!, $serviceId: String!
) {
  environment(id: $environmentId, projectId: $projectId) {
    id projectId configEtag
  }
  serviceInstance(environmentId: $environmentId, serviceId: $serviceId) {
    serviceId environmentId
    source { image repo }
    activeDeployments { id status projectId environmentId serviceId }
  }
  serviceInstanceAutoDeployStatus(
    projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId
  ) { enabled }
  deploymentTriggers(
    projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId,
    first: 100
  ) {
    pageInfo { hasNextPage }
    edges { node { id projectId environmentId serviceId branch repository provider } }
  }
}
"""

_IMAGE_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_PINNED_IMAGE = re.compile(
    r"(?P<registry>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?)/"
    rf"{_IMAGE_COMPONENT}(?:/{_IMAGE_COMPONENT})*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"@(?P<digest>sha256:[0-9a-f]{64})"
)


class EvidenceUnavailable(ValueError):
    """A sanitized error code, never an upstream payload or exception string."""


@dataclass(frozen=True)
class Target:
    project_id: str
    environment_id: str
    service_id: str

    def variables(self) -> dict[str, str]:
        return {
            "projectId": self.project_id,
            "environmentId": self.environment_id,
            "serviceId": self.service_id,
        }


def mapping(value: object) -> dict:
    if not isinstance(value, dict):
        raise EvidenceUnavailable("missing_or_invalid_object")
    return value


def sequence(value: object) -> list:
    if not isinstance(value, list):
        raise EvidenceUnavailable("missing_or_invalid_list")
    return value


def text_field(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2048
        or any(ord(char) < 32 for char in value)
    ):
        raise EvidenceUnavailable("missing_or_invalid_text")
    return value


def boolean(value: object) -> bool:
    if type(value) is not bool:
        raise EvidenceUnavailable("missing_or_invalid_boolean")
    return value


def same_identity(node: dict, expected: dict[str, str]) -> None:
    if any(node.get(key) != value for key, value in expected.items()):
        raise EvidenceUnavailable("target_identity_mismatch")


def configured_registry_digest(image: str | None) -> str | None:
    """Return only a full registry reference's SHA-256 pin, never runtime proof."""
    match = _PINNED_IMAGE.fullmatch(image or "")
    if match is None:
        return None
    registry = match.group("registry")
    if "." not in registry and ":" not in registry and registry != "localhost":
        return None
    return match.group("digest")


def read_source(value: object) -> dict:
    source = mapping(value)
    # The unused source alternative is legitimately null. Missing keys are not.
    if "image" not in source or "repo" not in source:
        raise EvidenceUnavailable("missing_source_field")
    image, repo = source["image"], source["repo"]
    if image is None and repo is None:
        raise EvidenceUnavailable("source_unavailable")
    for reference in (image, repo):
        if reference is not None:
            text_field(reference)
            # Source references are names, never authenticated URLs.
            if "://" in reference or any(char.isspace() for char in reference):
                raise EvidenceUnavailable("invalid_source_reference")
    if image is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]*(?:@sha256:[0-9a-fA-F]+)?", image
    ):
        raise EvidenceUnavailable("invalid_image_reference")
    if repo is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise EvidenceUnavailable("invalid_repository_reference")
    return {
        "image": image,
        "repo": repo,
        "configured_registry_digest": configured_registry_digest(image),
    }


def read_triggers(value: object, target: Target) -> list[dict]:
    connection = mapping(value)
    page = mapping(connection.get("pageInfo"))
    if boolean(page.get("hasNextPage")):
        raise EvidenceUnavailable("incomplete_trigger_listing")
    triggers = []
    for edge in sequence(connection.get("edges")):
        node = mapping(mapping(edge).get("node"))
        same_identity(node, target.variables())
        triggers.append(
            {
                key: text_field(node.get(key))
                for key in ("id", "branch", "repository", "provider")
            }
        )
    return triggers


def read_deployments(value: object, target: Target) -> list[dict]:
    deployments = []
    for item in sequence(value):
        node = mapping(item)
        same_identity(node, target.variables())
        deployments.append({key: text_field(node.get(key)) for key in ("id", "status")})
    if len({item["id"] for item in deployments}) != len(deployments):
        raise EvidenceUnavailable("duplicate_deployment_identity")
    return deployments


def observe(payload: object, target: Target) -> dict:
    response = mapping(payload)
    if response.get("errors"):
        raise EvidenceUnavailable("railway_api_errors")
    data = mapping(response.get("data"))
    environment = mapping(data.get("environment"))
    same_identity(
        environment, {"id": target.environment_id, "projectId": target.project_id}
    )
    etag = text_field(environment.get("configEtag"))
    service = mapping(data.get("serviceInstance"))
    same_identity(
        service,
        {"serviceId": target.service_id, "environmentId": target.environment_id},
    )
    source = read_source(service.get("source"))
    auto_deploy = mapping(data.get("serviceInstanceAutoDeployStatus"))
    enabled = boolean(auto_deploy.get("enabled"))
    triggers = read_triggers(data.get("deploymentTriggers"), target)
    deployments = read_deployments(service.get("activeDeployments"), target)

    source_blockers = []
    if source["repo"] is not None:
        source_blockers.append("repository_source_configured")
    if source["configured_registry_digest"] is None:
        source_blockers.append("registry_digest_not_pinned")
    if enabled:
        source_blockers.append("branch_auto_deploy_enabled")
    if triggers:
        source_blockers.append("branch_deployment_triggers_present")
    blockers = list(source_blockers)
    if len(deployments) != 1:
        blockers.append("active_deployment_not_unique")
    if any(item["status"] != "SUCCESS" for item in deployments):
        blockers.append("active_deployment_not_successful")
    blockers.extend(
        [
            "image_auto_updates_unverified",
            "runtime_artifact_unverified",
            "exclusive_deployment_authority_unverified",
        ]
    )
    return {
        "evidence_status": "available",
        "readiness": "not_ready",
        "config_etag": etag,
        "configured_source": source,
        "configured_source_ready": not source_blockers,
        "branch_auto_deploy": {"enabled": enabled, "triggers": triggers},
        "active_deployments": deployments,
        "image_auto_updates": "unknown",
        "runtime_artifact_verified": False,
        "single_writer_ready": False,
        "blockers": blockers,
    }


def parse_response(raw: bytes) -> object:
    if len(raw) > MAX_INPUT_BYTES:
        raise EvidenceUnavailable("response_too_large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceUnavailable("invalid_json_response") from exc


def read_fixture(path: Path) -> object:
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise EvidenceUnavailable("fixture_unavailable") from exc
    return parse_response(raw)


def read_live(target: Target) -> object:
    try:
        result = subprocess.run(
            ["railway", "api", QUERY, "--variables", json.dumps(target.variables())],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # CLI stderr and GraphQL error details may contain private information.
        raise EvidenceUnavailable("railway_read_failed") from exc
    return parse_response(result.stdout)


def uuid_argument(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "an explicit Railway UUID is required"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "environment", "service"):
        parser.add_argument(f"--{name}", required=True, type=uuid_argument)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", type=Path, help="offline GraphQL response fixture")
    mode.add_argument("--live", action="store_true", help="read Railway through CLI")
    args = parser.parse_args(argv)
    target = Target(args.project, args.environment, args.service)
    report = {
        "schema_version": 1,
        "observation_mode": "live_read_only" if args.live else "offline_fixture",
        "observed_at": None,
        "target": asdict(target),
    }
    try:
        payload = read_live(target) if args.live else read_fixture(args.input)
        report.update(observe(payload, target))
        if args.live:
            report["observed_at"] = datetime.now(timezone.utc).isoformat()
        code = 1
    except EvidenceUnavailable as exc:
        report.update(
            evidence_status="unavailable",
            readiness="evidence_unavailable",
            configured_source_ready=False,
            runtime_artifact_verified=False,
            single_writer_ready=False,
            blockers=[str(exc)],
        )
        code = 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
