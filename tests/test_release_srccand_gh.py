"""Offline tests for GitHubSourceCandidateResolver (WP-562 Ф3.4).

Same pattern as test_release_checksuite_github.py: `candidate_ref`/`base_ref`
resolution runs against a real temp git repo (no GitHub involved there), and
`fetch_pull_requests`/`fetch_reviews` are injected as plain functions
returning canned API responses -- these tests exercise the resolver's own
staleness/independence logic, not GitHub's API or `gh` itself.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.release_control import SourceCandidateVerificationRequest
from scripts.release_source_candidate_github import (
    GitHubSourceCandidateResolver,
    SourceCandidateResolutionError,
)

_REPOSITORY = "aisystant/aist_bot"


class _FakeClock:
    def __init__(self, now: datetime.datetime | None = None) -> None:
        self.now = now or datetime.datetime(
            2026, 10, 14, 18, 0, 0, tzinfo=datetime.UTC
        )

    def now_utc(self) -> datetime.datetime:
        return self.now


def _make_request(*, source_commit: str, source_tree: str, **overrides):
    fields = {
        "repository": _REPOSITORY,
        "contract_digest": "sha256:" + "0" * 64,
        "contract_edition": "wp562-f1-c",
        "release_id": "test-release",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "embedded_manifest_hash": "sha256:" + "1" * 64,
        "artifact_digest": "sha256:" + "2" * 64,
        "candidate_ref": "pilot",
        "base_ref": "main",
        "check_suite_receipt_hash": "sha256:" + "3" * 64,
    }
    fields.update(overrides)
    return SourceCandidateVerificationRequest(**fields)


def _pull(
    number: int,
    author_login: str,
    *,
    head_ref: str = "pilot",
    base_ref: str = "main",
    state: str = "open",
) -> dict:
    return {
        "number": number,
        "user": {"login": author_login},
        "head": {"ref": head_ref},
        "base": {"ref": base_ref},
        "state": state,
    }


def _review(login: str, *, state: str = "APPROVED", commit_id: str) -> dict:
    return {"user": {"login": login}, "state": state, "commit_id": commit_id}


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wp562-source-candidate-")
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
        (self.repo / "app.py").write_text("print('base')\n", encoding="utf-8")
        self._git("add", "--", "app.py")
        self._git("commit", "-m", "base commit")
        self.base_commit = self._git("rev-parse", "HEAD").strip()
        self._git("checkout", "-b", "pilot")
        (self.repo / "app.py").write_text("print('candidate')\n", encoding="utf-8")
        self._git("commit", "-am", "candidate commit")
        self.candidate_commit = self._git("rev-parse", "HEAD").strip()
        self.candidate_tree = self._git(
            "rev-parse", f"{self.candidate_commit}^{{tree}}"
        ).strip()

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=10,
        ).stdout

    def _resolver(self, *, pulls, reviews):
        return GitHubSourceCandidateResolver(
            repo_root=str(self.repo),
            repository=_REPOSITORY,
            clock=_FakeClock(),
            # Test repos are local-only (no remote) -- a real `git fetch`
            # would fail here, and isn't what these tests exercise.
            fetch_refs=lambda *args, **kwargs: None,
            fetch_pull_requests=lambda _path: pulls,
            fetch_reviews=lambda _path: reviews,
        )

    def test_fresh_reviewed_candidate_returns_complete_receipt(self):
        request = _make_request(
            source_commit=self.candidate_commit, source_tree=self.candidate_tree
        )
        resolver = self._resolver(
            pulls=[_pull(42, "author-login")],
            reviews=[_review("reviewer-login", commit_id=self.candidate_commit)],
        )

        receipt = resolver.resolve_verified(request)

        self.assertTrue(receipt.complete)
        self.assertTrue(receipt.candidate_ref_reachable)
        self.assertTrue(receipt.independent_review)
        self.assertEqual(receipt.candidate_head_commit, self.candidate_commit)
        self.assertEqual(receipt.base_commit, self.base_commit)
        self.assertEqual(receipt.reviewer_identity, "reviewer-login")
        self.assertEqual(
            receipt.verifier_identity, "github-source-candidate-resolver.v1"
        )

    def test_ref_moved_past_requested_commit_raises(self):
        # source_commit is the OLD candidate tip; candidate_ref ("pilot") has
        # since moved to self.candidate_commit -- this is exactly the
        # staleness case the resolver must refuse, not silently accept
        # because the old commit is still reachable in history.
        request = _make_request(
            source_commit=self.base_commit, source_tree=self.base_commit
        )
        resolver = self._resolver(
            pulls=[_pull(42, "author-login")],
            reviews=[_review("reviewer-login", commit_id=self.base_commit)],
        )

        with self.assertRaisesRegex(SourceCandidateResolutionError, "ref has moved"):
            resolver.resolve_verified(request)

    def test_no_pull_request_found_raises(self):
        request = _make_request(
            source_commit=self.candidate_commit, source_tree=self.candidate_tree
        )
        resolver = self._resolver(pulls=[], reviews=[])

        with self.assertRaisesRegex(
            SourceCandidateResolutionError, "no open pull request"
        ):
            resolver.resolve_verified(request)

    def test_multiple_pull_requests_is_ambiguous_and_raises(self):
        request = _make_request(
            source_commit=self.candidate_commit, source_tree=self.candidate_tree
        )
        resolver = self._resolver(
            pulls=[_pull(42, "author-login"), _pull(43, "author-login")],
            reviews=[],
        )

        with self.assertRaisesRegex(
            SourceCandidateResolutionError, "expected exactly one"
        ):
            resolver.resolve_verified(request)

    def test_review_against_earlier_commit_does_not_count(self):
        # Simulates a force-push after approval: the APPROVED review's
        # commit_id is an earlier commit, not the one actually being
        # promoted.
        request = _make_request(
            source_commit=self.candidate_commit, source_tree=self.candidate_tree
        )
        resolver = self._resolver(
            pulls=[_pull(42, "author-login")],
            reviews=[_review("reviewer-login", commit_id=self.base_commit)],
        )

        with self.assertRaisesRegex(
            SourceCandidateResolutionError, "no independent APPROVED review"
        ):
            resolver.resolve_verified(request)

    def test_review_by_author_is_not_independent(self):
        request = _make_request(
            source_commit=self.candidate_commit, source_tree=self.candidate_tree
        )
        resolver = self._resolver(
            pulls=[_pull(42, "author-login")],
            reviews=[_review("author-login", commit_id=self.candidate_commit)],
        )

        with self.assertRaisesRegex(
            SourceCandidateResolutionError, "no independent APPROVED review"
        ):
            resolver.resolve_verified(request)

    def test_non_approved_review_does_not_count(self):
        request = _make_request(
            source_commit=self.candidate_commit, source_tree=self.candidate_tree
        )
        resolver = self._resolver(
            pulls=[_pull(42, "author-login")],
            reviews=[
                _review(
                    "reviewer-login",
                    state="CHANGES_REQUESTED",
                    commit_id=self.candidate_commit,
                )
            ],
        )

        with self.assertRaisesRegex(
            SourceCandidateResolutionError, "no independent APPROVED review"
        ):
            resolver.resolve_verified(request)


    def test_pull_request_with_wrong_base_ref_is_ignored(self):
        # A PR whose head matches but whose base does not is a different
        # promotion (or a rebase-onto-another-branch artifact) -- its
        # approvals must not certify this candidate_ref/base_ref pair.
        request = _make_request(
            source_commit=self.candidate_commit, source_tree=self.candidate_tree
        )
        resolver = self._resolver(
            pulls=[_pull(42, "author-login", base_ref="some-other-branch")],
            reviews=[_review("reviewer-login", commit_id=self.candidate_commit)],
        )

        with self.assertRaisesRegex(
            SourceCandidateResolutionError, "no open pull request"
        ):
            resolver.resolve_verified(request)

    def test_closed_pull_request_is_ignored(self):
        request = _make_request(
            source_commit=self.candidate_commit, source_tree=self.candidate_tree
        )
        resolver = self._resolver(
            pulls=[_pull(42, "author-login", state="closed")],
            reviews=[_review("reviewer-login", commit_id=self.candidate_commit)],
        )

        with self.assertRaisesRegex(
            SourceCandidateResolutionError, "no open pull request"
        ):
            resolver.resolve_verified(request)

    def test_fetch_failure_raises_before_any_rev_parse(self):
        request = _make_request(
            source_commit=self.candidate_commit, source_tree=self.candidate_tree
        )

        def _failing_fetch(*_args, **_kwargs):
            raise subprocess.CalledProcessError(
                1, ["git", "fetch"], stderr="network unreachable"
            )

        resolver = GitHubSourceCandidateResolver(
            repo_root=str(self.repo),
            repository=_REPOSITORY,
            clock=_FakeClock(),
            fetch_refs=_failing_fetch,
            fetch_pull_requests=lambda _path: (_ for _ in ()).throw(
                AssertionError("should not reach PR lookup after a failed fetch")
            ),
        )

        with self.assertRaisesRegex(SourceCandidateResolutionError, "git fetch failed"):
            resolver.resolve_verified(request)


if __name__ == "__main__":
    unittest.main()
