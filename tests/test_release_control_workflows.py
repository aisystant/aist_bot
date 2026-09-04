"""Static fail-closed checks for release-critical GitHub workflow code."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CONTRACT = REPO_ROOT / ".github" / "release-control-contract.json"
SHADOW_ACTION = (
    REPO_ROOT / ".github" / "actions" / "reject-python-shadows" / "action.yml"
)
PINNED_ACTION = re.compile(r"[^@\s]+@[0-9a-f]{40}\Z")
UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def _workflow_sources(directory: Path = WORKFLOWS) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml")))
    }


def _workflow_document(source: str) -> dict[str, object]:
    document = yaml.load(source, Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document


def _uses_values(value: object) -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, child in value.items():
            if key == "uses":
                assert isinstance(child, str)
                found.append(child)
            found.extend(_uses_values(child))
        return found
    if isinstance(value, list):
        found = []
        for child in value:
            found.extend(_uses_values(child))
        return found
    return []


def _uses_is_immutable(value: str) -> bool:
    if value.startswith("docker://"):
        return False
    if value.startswith("./"):
        return "@" not in value
    return PINNED_ACTION.fullmatch(value) is not None


def _permissions_do_not_elevate(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    return all(
        permission == "none" or (scope == "contents" and permission == "read")
        for scope, permission in value.items()
    )


def _run_shadow_action(
    repository: Path, revision: str
) -> subprocess.CompletedProcess[str]:
    action = yaml.load(
        SHADOW_ACTION.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    script = action["runs"]["steps"][0]["run"]
    environment = os.environ.copy()
    environment["GITHUB_SHA"] = revision
    return subprocess.run(
        ["bash", "-c", script],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_every_workflow_has_explicit_read_only_default_permissions() -> None:
    for name, source in _workflow_sources().items():
        document = _workflow_document(source)
        assert document.get("permissions") == {"contents": "read"}, (
            f"{name} must declare read-only default permissions"
        )
        jobs = document.get("jobs")
        assert isinstance(jobs, dict), f"{name} must declare jobs"
        for job_name, job in jobs.items():
            assert isinstance(job, dict), f"{name}:{job_name} must be a job mapping"
            assert _permissions_do_not_elevate(job.get("permissions")), (
                f"{name}:{job_name} elevates default permissions"
            )


def test_third_party_actions_are_immutable_commit_pins() -> None:
    for name, source in _workflow_sources().items():
        action_refs = _uses_values(_workflow_document(source))
        assert action_refs, f"{name} has no auditable action references"
        assert all(_uses_is_immutable(ref) for ref in action_refs), (
            f"{name} contains a mutable, Docker, or malformed action ref: {action_refs}"
        )


def test_workflow_discovery_includes_yaml_extension(tmp_path: Path) -> None:
    (tmp_path / "fixture.yaml").write_text(
        "permissions:\n  contents: read\njobs: {}\n",
        encoding="utf-8",
    )

    assert set(_workflow_sources(tmp_path)) == {"fixture.yaml"}


@pytest.mark.parametrize(
    "uses",
    ["docker://alpine:3.22", "actions/checkout@v4", "owner/action", "./local@main"],
)
def test_unpinned_and_docker_uses_are_rejected(uses: str) -> None:
    assert _uses_is_immutable(uses) is False


@pytest.mark.parametrize(
    "permissions",
    ["write-all", "read-all", {"contents": "write"}, {"id-token": "write"}],
)
def test_job_permissions_cannot_raise_the_workflow_default(
    permissions: object,
) -> None:
    assert _permissions_do_not_elevate(permissions) is False


def test_checkout_never_persists_a_repository_credential() -> None:
    for name, source in _workflow_sources().items():
        lines = source.splitlines()
        for index, line in enumerate(lines):
            if "uses: actions/checkout@" in line:
                checkout_block = "\n".join(lines[index : index + 6])
                assert "persist-credentials: false" in checkout_block, name


def test_python_jobs_reject_startup_shadows_before_interpreter_start() -> None:
    expected_uses = {
        "pilot-prod-sync.yml": 1,
        "release-control-dry-run.yml": 2,
        "scenario-compliance.yml": 1,
        "security.yml": 2,
        "smoke-tests.yml": 2,
    }
    action_ref = "uses: ./.github/actions/reject-python-shadows"
    workflows = _workflow_sources()
    for name, expected_count in expected_uses.items():
        assert workflows[name].count(action_ref) == expected_count, name

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    action_source = SHADOW_ACTION.read_text(encoding="utf-8")
    for forbidden_path in contract["forbidden_paths"]:
        assert f"          {forbidden_path}\n" in action_source
    assert "python scripts/" not in "\n".join(workflows.values())
    assert "python -m pytest" not in "\n".join(workflows.values())


def test_shadow_action_fails_closed_when_candidate_ref_is_missing(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(
        ["git", "-C", str(repository), "init", "-q"],
        check=True,
        capture_output=True,
    )
    result = _run_shadow_action(repository, "missing-candidate-ref")

    assert result.returncode != 0


def test_shadow_action_rejects_a_committed_stdlib_shadow(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(
        ["git", "-C", str(repository), "init", "-q"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "WP562 Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "wp562@example.invalid",
        ],
        check=True,
    )
    (repository / "hashlib.py").write_text("raise RuntimeError\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", "--", "hashlib.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "shadow fixture"],
        check=True,
    )

    result = _run_shadow_action(repository, "HEAD")

    assert result.returncode != 0
    assert "forbidden Python startup path: hashlib.py" in result.stderr


def test_release_entrypoints_ignore_package_precedence_and_stdlib_shadows(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "candidate"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    for relative_path in (
        "release_manifest.py",
        "scripts/release_control.py",
        "scripts/build_release_manifest.py",
        "scripts/check_release_metadata.py",
    ):
        source = REPO_ROOT / relative_path
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    (repository / "hashlib.py").write_text(
        'raise RuntimeError("untrusted hashlib shadow executed")\n',
        encoding="utf-8",
    )
    release_package = repository / "release_manifest"
    release_package.mkdir()
    (release_package / "__init__.py").write_text(
        'raise RuntimeError("untrusted release package executed")\n',
        encoding="utf-8",
    )
    release_control_package = scripts / "release_control"
    release_control_package.mkdir()
    (release_control_package / "__init__.py").write_text(
        'raise RuntimeError("untrusted release-control package executed")\n',
        encoding="utf-8",
    )

    for relative_path in (
        "scripts/release_control.py",
        "scripts/build_release_manifest.py",
        "scripts/check_release_metadata.py",
    ):
        result = subprocess.run(
            [sys.executable, "-I", str(repository / relative_path), "--help"],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (relative_path, result.stderr)
        assert "untrusted" not in result.stdout + result.stderr


def test_workflows_exclude_known_release_bypasses() -> None:
    forbidden = {
        "pull_request_target": "untrusted pull requests with elevated context",
        "continue-on-error: true": "a release gate cannot be advisory",
        "paths-ignore:": "release-critical changes cannot skip checks",
        "git push": "workflows cannot silently mutate repository refs",
        "FORCE_PROD": "the local production bypass is not a workflow authority",
        "RAILWAY_API_TOKEN": "ordinary workflows cannot receive mutation tokens",
        "deploymentRestart": "runtime restart is a release mutation",
        "railway redeploy": "direct provider mutation bypasses the runner",
        "railway up": "direct provider mutation bypasses the runner",
    }
    for name, source in _workflow_sources().items():
        for needle, reason in forbidden.items():
            assert needle not in source, f"{name}: {reason}"


def test_smoke_workflow_contains_non_skippable_full_regression() -> None:
    source = _workflow_sources()["smoke-tests.yml"]
    assert "name: full-regression" in source
    assert "name: l1-l2-smoke-regression" in source
    assert "python -I -m pytest --import-mode=importlib tests/" in source
    assert "needs: smoke-tests" in source
    assert "Post-Deploy Health Check" not in source


def test_scenario_check_is_artifact_only_and_green_is_mandatory() -> None:
    source = _workflow_sources()["scenario-compliance.yml"]
    document = _workflow_document(source)
    steps = document["jobs"]["check-scenarios"]["steps"]
    steps_by_name = {step["name"]: step for step in steps}
    assert "actions/upload-artifact@" in source
    assert "pull_request:" in source
    assert "branches: [pilot, new-architecture]" in source
    assert "CHECK_EXIT: ${{ steps.check.outputs.check_exit }}" in source
    assert "SCENARIO_STATUS: ${{ steps.check.outputs.status }}" in source
    assert 'test "${CHECK_EXIT}" = "0"' in source
    assert 'test "${SCENARIO_STATUS}" = "green"' in source
    assert 'test "${{ steps.check.outputs.' not in source
    assert "jq -er" in source
    assert "Commit report to repository" not in source
    for step_name in (
        "Generate report file",
        "Upload report as artifact",
        "Require a green result",
    ):
        assert steps_by_name[step_name]["if"] == "always()"
    assert "set -euo pipefail" in steps_by_name["Require a green result"]["run"]


def test_public_contract_is_target_agnostic_and_cutover_is_disabled() -> None:
    source = CONTRACT.read_text(encoding="utf-8")
    contract = json.loads(source)

    assert UUID.search(source) is None
    assert "http://" not in source
    assert "https://" not in source
    assert contract["cutover"] == {
        "enabled": False,
        "negative_matrix_required": True,
        "negative_matrix_max_age_seconds": 86400,
        "negative_matrix_evidence_ref": None,
    }
    assert contract["promotion"]["build_once"] is True
    assert contract["promotion"]["check_suite_receipt_required"] is True
    assert contract["promotion"]["check_suite_max_age_seconds"] == 3600
    assert contract["promotion"]["source_candidate_receipt_required"] is True
    assert contract["promotion"]["source_candidate_max_age_seconds"] == 3600
    assert contract["promotion"]["independent_review_required"] is True
    assert (
        contract["promotion"]["pilot_qualification_independent_verifier_required"]
        is True
    )
    assert contract["promotion"]["pilot_qualification_max_age_seconds"] == 300
    assert contract["promotion"]["pilot_qualification_receipt_required"] is True
    assert contract["promotion"]["pilot_qualification_required_signals"] == [
        "canary",
        "data-invariants",
        "readiness",
        "runtime-attestation",
    ]
    assert contract["promotion"]["provider_deadline_required"] is True
    assert contract["promotion"]["same_digest_required"] is True
    assert contract["promotion"]["provider_idempotency_required"] is True
    assert contract["promotion"]["provider_fencing_required"] is True
    assert contract["promotion"]["zero_settlement_observations"] == 2
    assert contract["ledger"]["target_version_cas_required"] is True
    assert contract["ledger"]["raw_evidence_forbidden"] is True
    assert contract["rollback"]["compatibility_max_age_seconds"] == 1800
    assert contract["rollback"]["compatibility_receipt_required"] is True
    assert contract["rollback"]["historical_build_contract_receipt_required"] is True
    assert contract["rollback"]["independent_verifier_required"] is True
    assert contract["authority"]["purpose"] == "iwe.release-control.authority.v1"
    assert contract["authority"]["audience"] == "iwe.release-control.runner"
    assert contract["authority"]["trust_root_identity"] == "release-authority-root"
    assert contract["artifact"]["digest_kind"] == "oci_manifest"
    assert contract["artifact"]["oci_repository"] == (
        "registry.example.invalid/team/app"
    )
    assert contract["artifact"]["platform"] == {
        "os": "linux",
        "architecture": "amd64",
    }
    assert "pilot-production-semantic-parity" in contract["required_checks"]
    assert "release-candidate-build" in contract["required_checks"]
    assert contract["forbidden_paths"] == sorted(set(contract["forbidden_paths"]))
    assert not set(contract["forbidden_paths"]) & set(contract["protected_paths"])


def test_parity_gate_evaluates_the_proposed_ref_before_merge() -> None:
    source = _workflow_sources()["pilot-prod-sync.yml"]

    assert 'pilot) pilot_ref="${GITHUB_SHA}"' in source
    assert 'new-architecture) production_ref="${GITHUB_SHA}"' in source
    assert "name: pilot-production-semantic-parity" in source


def test_every_protected_path_exists_in_the_candidate_tree() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    for path_pattern in contract["protected_paths"]:
        candidates = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                path_pattern,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert candidates, path_pattern
        for relative_path in candidates:
            metadata = (REPO_ROOT / relative_path).lstat()
            assert stat.S_ISREG(metadata.st_mode), relative_path
            indexed = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "ls-files", "-s", "--", relative_path],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if indexed:
                mode, _object_id, stage_and_path = indexed[0].split(maxsplit=2)
                stage, indexed_path = stage_and_path.split("\t", maxsplit=1)
                assert indexed_path == relative_path
                assert stage == "0", relative_path
                assert mode in {"100644", "100755"}, relative_path
            else:
                # New protected files are untracked only in a local pre-commit
                # run. A clean CI checkout must obtain every entry from Git.
                assert os.environ.get("CI") != "true", relative_path


def test_contract_protects_the_release_control_trust_boundary() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    protected = set(contract["protected_paths"])

    assert {
        ".dockerignore",
        ".gitattributes",
        ".githooks/pre-push",
        ".github/CODEOWNERS",
        ".github/actions/reject-python-shadows/action.yml",
        ".github/release-metadata.json",
        ".github/workflows/check-dt-indicators.yml",
        ".github/workflows/release-control-dry-run.yml",
        ".github/workflows/security.yml",
        ".github/workflows/smoke-tests.yml",
        ".gitmodules",
        "CLAUDE.md",
        "Dockerfile",
        "PROCESSES.md",
        "bot.py",
        "clients/github_content.py",
        "config/settings.py",
        "conftest.py",
        "core/autofix.py",
        "db/queries/autofix.py",
        "oauth_server.py",
        "pytest.ini",
        "release_attestation.py",
        "release_manifest.py",
        "requirements.in",
        "scripts/build_release_manifest.py",
        "scripts/check_release_metadata.py",
        "scripts/release_control.py",
        "tests/__init__.py",
        "tests/test_autofix_security.py",
        "tests/test_release_attestation.py",
        "tests/test_release_control.py",
        "tests/test_release_control_workflows.py",
        "tests/test_readiness.py",
        "tests/test_release_metadata.py",
        "tests/test_repo/requirements-scenarios.yaml",
        "tests/test_repo/scripts/checker.py",
        "tests/test_repo/scripts/code_analyzer.py",
        "tests/test_repo/scripts/report_generator.py",
        "wheels/activity_hub-0.1.0-py3-none-any.whl",
    } <= protected

    for forbidden_path in contract["forbidden_paths"]:
        assert not (REPO_ROOT / forbidden_path).exists(), forbidden_path


def test_codeowners_covers_release_control_and_runtime_mutation_paths() -> None:
    source = (REPO_ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")

    for pattern in (
        "/.github/CODEOWNERS ",
        "/.github/actions/reject-python-shadows/ ",
        "/.github/release-control-contract.json ",
        "/.github/release-metadata.json ",
        "/.github/workflows/ ",
        "/.gitattributes ",
        "/.gitmodules ",
        "/bot.py ",
        "/clients/github_content.py ",
        "/config/settings.py ",
        "/conftest.py ",
        "/core/autofix.py ",
        "/db/queries/autofix.py ",
        "/hashlib.py ",
        "/hashlib/ ",
        "/requirements.in ",
        "/scripts/release_control.py ",
        "/scripts/check_release_metadata.py ",
        "/release_manifest/ ",
        "/scripts.py ",
        "/scripts/release_control/ ",
        "/sitecustomize.py ",
        "/tests/conftest.py ",
        "/tests/test_autofix_security.py ",
        "/tests/test_repo/requirements-scenarios.yaml ",
        "/tests/test_repo/scripts/ ",
        "/wheels/ ",
    ):
        assert pattern in source
    assert "@TserenTserenov" in source


def test_autofix_defaults_to_pilot_and_refuses_any_other_write_base() -> None:
    settings_source = (REPO_ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    autofix_source = (REPO_ROOT / "core" / "autofix.py").read_text(encoding="utf-8")

    assert 'os.getenv("AUTOFIX_BRANCH_BASE", "pilot")' in settings_source
    guard = 'if AUTOFIX_BRANCH_BASE != "pilot":'
    assert guard in autofix_source
    assert autofix_source.index(guard) < autofix_source.index(
        "async with aiohttp.ClientSession() as session:",
        autofix_source.index("async def apply_fix"),
    )


def test_attestation_snapshot_is_loaded_before_request_handling() -> None:
    source = (REPO_ROOT / "oauth_server.py").read_text(encoding="utf-8")

    module_import = "from release_attestation import packaged_attestation_snapshot"
    handler = "async def attestation_handler"
    assert source.count(module_import) == 1
    assert source.index(module_import) < source.index(handler)


def test_docker_build_has_a_pinned_linux_manifest_and_minimal_context() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert re.findall(
        r"(?m)^FROM python:3\.11-slim@sha256:[0-9a-f]{64} AS (build|runtime-base)$",
        dockerfile,
    ) == ["build", "runtime-base"]
    assert dockerfile.count("FROM python:3.11-slim@sha256:") == 2
    assert " AS build" in dockerfile
    assert "FROM runtime-base AS development" in dockerfile
    assert "FROM runtime-base AS runtime" in dockerfile
    assert dockerfile.rfind("FROM runtime-base AS development") > dockerfile.rfind(
        "FROM runtime-base AS runtime"
    )
    assert "ARG RELEASE_MANIFEST_SOURCE=" not in dockerfile
    assert "COPY ." not in dockerfile
    assert "COPY release_manifest.py ." in dockerfile
    assert "COPY release_attestation.py ." in dockerfile
    assert ("COPY ${RELEASE_MANIFEST_SOURCE} ./release-manifest.json") in dockerfile
    wheel = REPO_ROOT / "wheels" / "activity_hub-0.1.0-py3-none-any.whl"
    wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert wheel_sha256 in dockerfile
    assert "pip install --no-cache-dir --no-deps wheels/activity_hub" in dockerfile
    assert dockerfile.index("pip install --no-cache-dir -r requirements.txt") < (
        dockerfile.index("pip install --no-cache-dir --no-deps wheels/activity_hub")
    )
    assert "pip check" in dockerfile
    runtime_stage = dockerfile.split("FROM runtime-base AS runtime", maxsplit=1)[1]
    assert "gcc" not in runtime_stage
    assert "git" not in runtime_stage
    assert dockerfile.count("USER 10001:10001") == 2
    assert "--chown=10001:10001" not in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "find /app -type d -exec chmod 0555" in dockerfile
    assert "find /app -type f -exec chmod 0444" in dockerfile
    assert dockerfile.count("RUN chmod 0444 ./release-manifest.json") == 2
    assert (
        "COPY release-manifest.unavailable.json ./release-manifest.json"
    ) in dockerfile
    assert dockerignore.startswith("**\n")
    assert "!.env" not in dockerignore.splitlines()
    assert "!.git" not in dockerignore.splitlines()


def test_dry_run_builds_only_the_exact_committed_source_tree() -> None:
    source = _workflow_sources()["release-control-dry-run.yml"]

    assert "git archive" not in source
    assert "--context-output wp562-build-context" in source
    assert "--metadata .github/release-metadata.json" in source
    assert "scripts/check_release_metadata.py" in source
    assert "--base-ref" in source
    assert "--head-ref" in source
    assert "--migration-class" not in source
    assert "--schema-min" not in source
    assert "--schema-max" not in source
    assert "wp562-build-context/release-manifest.candidate.json" in source
    assert "name: release-candidate-build" in source
    assert "needs: release-control-contract" in source
    assert "--target runtime" in source
    assert "EXPECTED_MANIFEST_SHA256" in source
    assert source.index("Exercise release planning") < source.index(
        "Generate manifest from immutable Git blobs"
    )
    assert re.search(r"(?m)^\s+wp562-build-context\s*$", source)


def test_manual_dry_run_fails_before_using_an_untrusted_baseline() -> None:
    source = _workflow_sources()["release-control-dry-run.yml"]
    document = _workflow_document(source)
    steps = document["jobs"]["release-control-contract"]["steps"]
    steps_by_name = {step["name"]: step for step in steps}
    guard = steps_by_name["Reject an unanchored manual baseline"]

    assert guard["if"] == "github.event_name == 'workflow_dispatch'"
    assert "exit 1" in guard["run"]
    assert source.index("Reject an unanchored manual baseline") < source.index(
        "Validate committed migration declaration"
    )


def test_runtime_and_local_hook_have_no_direct_mutation_escape() -> None:
    health_source = (REPO_ROOT / "core" / "health_check.py").read_text(encoding="utf-8")
    hook_source = (REPO_ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")

    for forbidden in (
        "deploymentRestart",
        "Project-Access-Token",
        "RAILWAY_API_TOKEN",
    ):
        assert forbidden not in health_source
    assert "FORCE_PROD" not in hook_source
    assert "refs/heads/${PROTECTED_BRANCH}" in hook_source


def test_cutover_requires_disabling_both_branch_deploy_sources() -> None:
    process = (REPO_ROOT / "PROCESSES.md").read_text(encoding="utf-8")

    assert "только до cutover" in process
    assert "branch/source auto-deploy для обеих сред" in process
    assert "push в `pilot`, ни push в `new-architecture`" in process
    assert "единственным источником деплоя служит точный OCI digest" in process
    assert "Живой отрицательный тест" in process


def test_disabled_cutover_keeps_default_pilot_deployment_on_liveness() -> None:
    railway_config = json.loads(
        (REPO_ROOT / "railway.json").read_text(encoding="utf-8")
    )
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert contract["cutover"]["enabled"] is False
    assert railway_config["deploy"]["healthcheckPath"] == "/health"
    assert dockerfile.rfind("FROM runtime-base AS development") > dockerfile.rfind(
        "FROM runtime-base AS runtime"
    )
    assert "release-manifest.unavailable.json" in dockerfile
