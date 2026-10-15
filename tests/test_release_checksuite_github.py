"""Offline tests for GitHubCheckSuiteResolver (WP-562 Ф3.4).

forbidden_paths checks run against a real temp git repo (same pattern as
test_release_drift_detector.py) -- no GitHub involved there at all. The
GitHub Checks API is never called for real in these tests: `fetch_check_runs`
is injected as a plain function returning canned check-run dicts, so the
tests exercise GitHubCheckSuiteResolver's own matching/validation logic,
not GitHub's API or `gh` itself.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.release_checksuite_github import (
    CheckSuiteResolutionError,
    GitHubCheckSuiteResolver,
)
from scripts.release_control import CheckSuiteVerificationRequest

_REQUIRED_CHECKS = ("bandit-sast", "pip-audit")


class _FakeClock:
    """Same shape as release_control.py's own FakeClock test double."""

    def __init__(self, now: datetime.datetime | None = None) -> None:
        self.now = now or datetime.datetime(
            2026, 10, 14, 11, 0, 0, tzinfo=datetime.UTC
        )

    def now_utc(self) -> datetime.datetime:
        return self.now


def _make_request(source_commit: str, **overrides) -> CheckSuiteVerificationRequest:
    fields = {
        "repository": "aisystant/aist_bot",
        "contract_digest": "sha256:" + "0" * 64,
        "contract_edition": "wp562-f1-c",
        "release_id": "test-release",
        "source_commit": source_commit,
        "source_tree": "0" * 40,
        "embedded_manifest_hash": "sha256:" + "1" * 64,
        "artifact_digest": "sha256:" + "2" * 64,
        "required_checks": _REQUIRED_CHECKS,
        "forbidden_paths": (),
    }
    fields.update(overrides)
    return CheckSuiteVerificationRequest(**fields)


def _check_run(
    name: str, *, head_sha: str, status: str = "completed", conclusion: str = "success"
) -> dict:
    return {
        "id": abs(hash((name, head_sha))) % 10_000_000,
        "name": name,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "started_at": "2026-10-14T10:00:00Z",
        "completed_at": "2026-10-14T10:05:00Z",
        "html_url": f"https://example.invalid/{name}",
        "check_suite": {"id": 1},
    }


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wp562-checksuite-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Offline Test")
        self._git("config", "user.email", "offline@example.invalid")
        (self.repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
        self._git("add", "--", "app.py")
        self._git("commit", "-m", "baseline")
        self.commit = self._git("rev-parse", "HEAD").strip()

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=10,
        ).stdout

    def test_all_checks_green_returns_complete_receipt(self):
        request = _make_request(self.commit)
        runs = [_check_run(name, head_sha=self.commit) for name in _REQUIRED_CHECKS]
        resolver = GitHubCheckSuiteResolver(
            repo_root=str(self.repo), clock=_FakeClock(), fetch_check_runs=lambda _path: runs
        )

        receipt = resolver.resolve_verified(request)

        self.assertTrue(receipt.complete)
        self.assertTrue(receipt.forbidden_paths_absent)
        self.assertEqual(
            {r.check_id for r in receipt.results}, set(_REQUIRED_CHECKS)
        )
        self.assertTrue(all(r.conclusion == "success" for r in receipt.results))
        self.assertEqual(
            len({r.provider_reference_hash for r in receipt.results}),
            len(receipt.results),
        )

    def test_missing_check_raises(self):
        request = _make_request(self.commit)
        runs = [_check_run("bandit-sast", head_sha=self.commit)]  # pip-audit missing
        resolver = GitHubCheckSuiteResolver(
            repo_root=str(self.repo), clock=_FakeClock(), fetch_check_runs=lambda _path: runs
        )

        with self.assertRaisesRegex(CheckSuiteResolutionError, "never ran"):
            resolver.resolve_verified(request)

    def test_still_running_check_raises(self):
        request = _make_request(self.commit)
        runs = [
            _check_run("bandit-sast", head_sha=self.commit),
            _check_run("pip-audit", head_sha=self.commit, status="in_progress"),
        ]
        runs[1].pop("conclusion")  # GitHub omits conclusion while in progress
        resolver = GitHubCheckSuiteResolver(
            repo_root=str(self.repo), clock=_FakeClock(), fetch_check_runs=lambda _path: runs
        )

        with self.assertRaisesRegex(CheckSuiteResolutionError, "still running"):
            resolver.resolve_verified(request)

    def test_failed_check_raises(self):
        request = _make_request(self.commit)
        runs = [
            _check_run("bandit-sast", head_sha=self.commit),
            _check_run("pip-audit", head_sha=self.commit, conclusion="failure"),
        ]
        resolver = GitHubCheckSuiteResolver(
            repo_root=str(self.repo), clock=_FakeClock(), fetch_check_runs=lambda _path: runs
        )

        with self.assertRaisesRegex(CheckSuiteResolutionError, "did not succeed"):
            resolver.resolve_verified(request)

    def test_check_from_wrong_commit_is_ignored_and_counts_as_missing(self):
        request = _make_request(self.commit)
        runs = [
            _check_run("bandit-sast", head_sha=self.commit),
            _check_run("pip-audit", head_sha="0" * 40),  # wrong commit
        ]
        resolver = GitHubCheckSuiteResolver(
            repo_root=str(self.repo), clock=_FakeClock(), fetch_check_runs=lambda _path: runs
        )

        with self.assertRaisesRegex(CheckSuiteResolutionError, "never ran"):
            resolver.resolve_verified(request)

    def test_forbidden_path_present_in_tree_raises_before_any_api_call(self):
        self._git("mv", "app.py", "hashlib.py")
        self._git("commit", "-m", "shadow stdlib module")
        shadowed_commit = self._git("rev-parse", "HEAD").strip()
        request = _make_request(shadowed_commit, forbidden_paths=("hashlib.py",))

        def _fail_if_called(_path):
            raise AssertionError("should reject on forbidden path before fetching checks")

        resolver = GitHubCheckSuiteResolver(
            repo_root=str(self.repo), clock=_FakeClock(), fetch_check_runs=_fail_if_called
        )

        with self.assertRaisesRegex(CheckSuiteResolutionError, "forbidden path present"):
            resolver.resolve_verified(request)

    def test_runs_from_different_suites_are_rejected(self):
        # Required checks spread across two distinct GitHub check suites is
        # not a legitimate "one CI run for this commit" shape for this
        # contract -- reject rather than silently pick/merge a hash.
        request = _make_request(self.commit)
        run_a = _check_run("bandit-sast", head_sha=self.commit)
        run_a["check_suite"] = {"id": 2}
        run_b = _check_run("pip-audit", head_sha=self.commit)
        run_b["check_suite"] = {"id": 1}
        resolver = GitHubCheckSuiteResolver(
            repo_root=str(self.repo),
            clock=_FakeClock(),
            fetch_check_runs=lambda _path: [run_a, run_b],
        )

        with self.assertRaisesRegex(CheckSuiteResolutionError, "multiple"):
            resolver.resolve_verified(request)

    def test_same_suite_results_share_the_receipt_hash(self):
        request = _make_request(self.commit)
        runs = [_check_run(name, head_sha=self.commit) for name in _REQUIRED_CHECKS]
        resolver = GitHubCheckSuiteResolver(
            repo_root=str(self.repo), clock=_FakeClock(), fetch_check_runs=lambda _path: runs
        )

        receipt = resolver.resolve_verified(request)

        for result in receipt.results:
            self.assertEqual(result.suite_reference_hash, receipt.suite_reference_hash)


if __name__ == "__main__":
    unittest.main()
