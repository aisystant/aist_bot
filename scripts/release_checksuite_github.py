"""GitHub-backed VerifiedCheckSuiteResolverPort (WP-562 Ф3.4).

Peer-session 2026-10-14-09-wp562-f32-f35-payment-migration (Claude+Kimi+Codex).

Fail-closed by construction: `resolve_verified()` either returns a receipt
where every one of `contract.required_checks` completed with conclusion
"success" on the exact commit being promoted, or it raises. There is no
partial/incomplete receipt shape -- the caller (`_resolve_check_suite` in
release_control.py) treats any raised exception as a hard rejection anyway,
so a partial receipt would only add a second, weaker way to fail without
adding any real information.

Known, deliberate limitation (not a bug): three of the contract's eight
required_checks (scenario-compliance, full-regression,
pilot-production-semantic-parity) have no corresponding per-commit GitHub
check today -- scenario-compliance exists but runs on a schedule, not
per-commit; the other two don't exist as CI jobs at all. Until the pilot
decides how to close that gap, `resolve_verified()` will always raise
here -- correctly, since the suite genuinely is not complete. See
04-writer.md and 05-peer-*.md in this session for the investigation.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
import uuid
from collections.abc import Callable

from release_manifest import canonical_sha256
from scripts.release_control import (
    CheckResult,
    CheckSuiteReceipt,
    CheckSuiteVerificationRequest,
    TrustedClockPort,
)

VERIFIER_IDENTITY = "github-check-suite-resolver.v1"


class CheckSuiteResolutionError(RuntimeError):
    """Raised whenever the suite cannot be verified as complete and green."""


def _run_gh_api(path: str) -> list[dict]:
    """Paginate a GitHub REST endpoint that returns `{"<key>": [...]}`."""
    results: list[dict] = []
    page = 1
    while True:
        proc = subprocess.run(
            ["gh", "api", f"{path}?per_page=100&page={page}"],
            capture_output=True,
            check=True,
            text=True,
        )
        payload = json.loads(proc.stdout)
        items = payload.get("check_runs", [])
        results.extend(items)
        if len(items) < 100:
            return results
        page += 1


def _latest_run_per_check(
    runs: list[dict], required_checks: tuple[str, ...], source_commit: str
) -> dict[str, dict]:
    by_name: dict[str, dict] = {}
    for run in runs:
        if run.get("head_sha") != source_commit:
            continue
        name = run.get("name")
        if name not in required_checks:
            continue
        existing = by_name.get(name)
        if existing is None or run.get("started_at", "") > existing.get(
            "started_at", ""
        ):
            by_name[name] = run
    return by_name


class GitHubCheckSuiteResolver:
    """VerifiedCheckSuiteResolverPort backed by the real GitHub Checks API.

    `repo_root` is the local git checkout used for `forbidden_paths`
    presence checks (git cat-file against the exact commit tree -- no
    network call, no dependence on GitHub's own tree APIs). `clock` is the
    same TrustedClockPort every other resolver in this contract takes
    (release_control.py:_resolve_check_suite threads one through) -- using
    the local system clock directly here would desync `verified_at` from
    whatever clock the caller's staleness checks are keyed to.
    """

    def __init__(
        self,
        *,
        repo_root: str,
        clock: TrustedClockPort,
        fetch_check_runs: Callable[[str], list[dict]] = _run_gh_api,
    ) -> None:
        self._repo_root = repo_root
        self._clock = clock
        self._fetch_check_runs = fetch_check_runs

    def resolve_verified(
        self, request: CheckSuiteVerificationRequest
    ) -> CheckSuiteReceipt:
        for path in request.forbidden_paths:
            if self._forbidden_path_present(request.source_commit, path):
                raise CheckSuiteResolutionError(
                    f"forbidden path present at {request.source_commit}: {path}"
                )

        runs = self._fetch_check_runs(
            f"repos/{request.repository}/commits/{request.source_commit}/check-runs"
        )
        latest_by_name = _latest_run_per_check(
            runs, request.required_checks, request.source_commit
        )
        missing = [
            name for name in request.required_checks if name not in latest_by_name
        ]
        if missing:
            raise CheckSuiteResolutionError(
                f"required checks never ran on {request.source_commit}: {missing}"
            )
        not_done = [
            name
            for name, run in latest_by_name.items()
            if run.get("status") != "completed"
        ]
        if not_done:
            raise CheckSuiteResolutionError(
                f"required checks still running on {request.source_commit}: {not_done}"
            )
        failed = [
            name
            for name, run in latest_by_name.items()
            if run.get("conclusion") != "success"
        ]
        if failed:
            raise CheckSuiteResolutionError(
                f"required checks did not succeed on {request.source_commit}: {failed}"
            )

        suite_ids = sorted({run["check_suite"]["id"] for run in latest_by_name.values()})
        if len(suite_ids) != 1:
            raise CheckSuiteResolutionError(
                f"required checks on {request.source_commit} span multiple "
                f"GitHub check suites: {suite_ids}"
            )
        suite_reference_hash = canonical_sha256({"suite_id": suite_ids[0]})

        results = tuple(
            self._check_result(name, latest_by_name[name], suite_reference_hash)
            for name in request.required_checks
        )

        now = self._clock.now_utc()
        return CheckSuiteReceipt(
            schema_version=1,
            receipt_id=self._receipt_id(request),
            request_digest=request.request_digest,
            repository=request.repository,
            contract_digest=request.contract_digest,
            contract_edition=request.contract_edition,
            release_id=request.release_id,
            source_commit=request.source_commit,
            source_tree=request.source_tree,
            embedded_manifest_hash=request.embedded_manifest_hash,
            artifact_digest=request.artifact_digest,
            required_checks=request.required_checks,
            forbidden_paths=request.forbidden_paths,
            complete=True,
            forbidden_paths_absent=True,
            suite_reference_hash=suite_reference_hash,
            results=results,
            verified_at=now,
            verifier_identity=VERIFIER_IDENTITY,
            evidence_hash=canonical_sha256(
                {"request": request.to_mapping(), "suite_ids": suite_ids}
            ),
        )

    def _forbidden_path_present(self, source_commit: str, path: str) -> bool:
        result = subprocess.run(
            ["git", "-C", self._repo_root, "cat-file", "-e", f"{source_commit}:{path}"],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _check_result(
        name: str, run: dict, suite_reference_hash: str
    ) -> CheckResult:
        completed_at = datetime.datetime.fromisoformat(run["completed_at"])
        provider_reference_hash = hashlib.sha256(
            f"github-check-run:{run['id']}".encode()
        ).hexdigest()
        return CheckResult(
            check_id=name,
            conclusion=run["conclusion"],
            completed_at=completed_at,
            suite_reference_hash=suite_reference_hash,
            provider_reference_hash=f"sha256:{provider_reference_hash}",
            evidence_hash=canonical_sha256(
                {"check_run_id": run["id"], "html_url": run.get("html_url", "")}
            ),
        )

    @staticmethod
    def _receipt_id(request: CheckSuiteVerificationRequest) -> str:
        return str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"check-suite:{request.request_digest}")
        )
