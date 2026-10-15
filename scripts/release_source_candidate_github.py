"""GitHub/git-backed VerifiedSourceCandidateResolverPort (WP-562 Ф3.4).

Peer-session 2026-10-14-17-wp562-release-contour (Claude+Kimi+Codex).

Fail-closed by construction, same discipline as `GitHubCheckSuiteResolver`:
`resolve_verified()` either returns a receipt the caller
(`_resolve_source_candidate` in release_control.py) accepts, or it raises.
There is no partial/weak receipt shape.

What this resolver actually proves, and why each check exists (TOCTOU is the
threat model Kimi raised in this session -- a stale or substituted ref must
not look like a fresh, reviewed one):

1. `candidate_ref` (a branch name from the contract, e.g. "pilot") is
   resolved via a LOCAL `git rev-parse` against `repo_root` -- not via the
   GitHub API, which would add a second, independently-spoofable source of
   truth for "what does this ref point to right now". The resolved commit
   MUST equal `request.source_commit` exactly: if the branch has moved past
   the commit actually being promoted, this is a stale read and the
   operation must not proceed on an out-of-date candidate. There is no
   fallback to "reachable in history" -- an old commit is always reachable
   in history long after a branch has moved past it, so that check alone
   would not catch the staleness case this resolver exists to catch.
2. `base_ref` (the production branch) is resolved the same way, giving
   `base_commit`/`base_tree` for the receipt -- this is the target the
   candidate is being compared/reviewed against, not itself a promotion
   target.
3. Independent review is read from the GitHub PR Reviews API
   (`commit_id` field on each review -- the exact SHA that was HEAD when
   that specific review was submitted, not the PR's current head). A
   review is only accepted if its `commit_id` equals `request.source_commit`
   exactly: an APPROVED review submitted against an earlier commit (before
   a force-push) does not certify the commit actually being promoted.
4. The reviewer must be a different GitHub login than the PR author, and
   different from both this resolver's and the check-suite resolver's
   machine identities (the same cross-identity rule release_control.py
   already enforces at the call site; duplicated here defensively so a
   malformed GitHub response -- e.g. a review attributed to a bot -- fails
   the resolver's own construction, not just the caller's post-hoc check).
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable

from release_manifest import canonical_sha256
from scripts.release_checksuite_github import (
    VERIFIER_IDENTITY as CHECK_SUITE_VERIFIER_IDENTITY,
)
from scripts.release_control import (
    SourceCandidateReceipt,
    SourceCandidateVerificationRequest,
    TrustedClockPort,
)

VERIFIER_IDENTITY = "github-source-candidate-resolver.v1"


class SourceCandidateResolutionError(RuntimeError):
    """Raised whenever the candidate cannot be verified as fresh and reviewed."""


def _run_git(repo_root: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _fetch_refs(repo_root: str, remote: str, *refs: str) -> None:
    """Bring `repo_root`'s local refs up to date with `remote` before any
    `rev-parse` -- without this, a long-lived checkout whose last fetch
    predates a force-push past `source_commit` would resolve a locally
    cached, already-superseded commit (which can still happen to equal
    `source_commit`), defeating the entire staleness check below.
    """
    subprocess.run(
        ["git", "-C", repo_root, "fetch", "--prune", remote, *refs],
        capture_output=True,
        check=True,
        text=True,
    )


def _run_gh_api_paginated(path: str) -> list[dict]:
    """Paginate a GitHub REST endpoint that returns a bare JSON array.

    `commits/{sha}/pulls` and `pulls/{number}/reviews` both return bare
    arrays (not wrapped under a key, unlike `check-runs`), oldest page
    first, paginating at 30 items by default. A single-page read silently
    misses later pages -- for `reviews` in particular, a qualifying
    APPROVED review for the exact commit being promoted can be beyond the
    first page on a long-lived PR with many re-review cycles.
    """
    results: list[dict] = []
    page = 1
    separator = "&" if "?" in path else "?"
    while True:
        proc = subprocess.run(
            ["gh", "api", f"{path}{separator}per_page=100&page={page}"],
            capture_output=True,
            check=True,
            text=True,
        )
        payload = json.loads(proc.stdout)
        if not isinstance(payload, list):
            raise SourceCandidateResolutionError(
                f"unexpected response shape from {path}"
            )
        results.extend(payload)
        if len(payload) < 100:
            return results
        page += 1


class GitHubSourceCandidateResolver:
    """VerifiedSourceCandidateResolverPort backed by local git + GitHub PR reviews.

    `repo_root` is the local git checkout used to resolve `candidate_ref` and
    `base_ref` -- see module docstring point 1 for why this must be local,
    not a second GitHub API read. `clock` is the same TrustedClockPort every
    other resolver in this contract takes.
    """

    def __init__(
        self,
        *,
        repo_root: str,
        repository: str,
        clock: TrustedClockPort,
        remote: str = "origin",
        run_git: Callable[..., str] = _run_git,
        fetch_refs: Callable[..., None] = _fetch_refs,
        fetch_pull_requests: Callable[[str], list[dict]] = _run_gh_api_paginated,
        fetch_reviews: Callable[[str], list[dict]] = _run_gh_api_paginated,
    ) -> None:
        self._repo_root = repo_root
        self._repository = repository
        self._clock = clock
        self._remote = remote
        self._run_git = run_git
        self._fetch_refs = fetch_refs
        self._fetch_pull_requests = fetch_pull_requests
        self._fetch_reviews = fetch_reviews

    def resolve_verified(
        self, request: SourceCandidateVerificationRequest
    ) -> SourceCandidateReceipt:
        try:
            self._fetch_refs(
                self._repo_root, self._remote, request.candidate_ref, request.base_ref
            )
        except subprocess.CalledProcessError as exc:
            raise SourceCandidateResolutionError(
                f"git fetch failed, cannot prove {request.candidate_ref!r}/"
                f"{request.base_ref!r} are current: {exc.stderr.strip()}"
            ) from exc

        candidate_head_commit = self._rev_parse(request.candidate_ref)
        if candidate_head_commit != request.source_commit:
            raise SourceCandidateResolutionError(
                f"candidate ref {request.candidate_ref!r} points at "
                f"{candidate_head_commit}, not the requested source commit "
                f"{request.source_commit} -- ref has moved, stale read refused"
            )
        candidate_head_tree = self._rev_parse(f"{candidate_head_commit}^{{tree}}")
        if candidate_head_tree != request.source_tree:
            raise SourceCandidateResolutionError(
                "candidate commit tree does not match the requested source tree"
            )

        base_commit = self._rev_parse(request.base_ref)
        base_tree = self._rev_parse(f"{base_commit}^{{tree}}")

        pull_number, author_login = self._find_pull_request(request)
        reviewer_login = self._find_qualifying_review(
            pull_number, author_login, request.source_commit
        )

        now = self._clock.now_utc()
        review_reference_hash = canonical_sha256(
            {"pull_number": pull_number, "commit_id": request.source_commit}
        )
        return SourceCandidateReceipt(
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
            candidate_ref=request.candidate_ref,
            base_ref=request.base_ref,
            check_suite_receipt_hash=request.check_suite_receipt_hash,
            candidate_head_commit=candidate_head_commit,
            candidate_head_tree=candidate_head_tree,
            base_commit=base_commit,
            base_tree=base_tree,
            reviewed_source_commit=request.source_commit,
            reviewed_source_tree=request.source_tree,
            reviewed_base_commit=base_commit,
            reviewed_base_tree=base_tree,
            candidate_ref_reachable=True,
            independent_review=True,
            complete=True,
            reviewer_identity=reviewer_login,
            verifier_identity=VERIFIER_IDENTITY,
            review_reference_hash=review_reference_hash,
            verified_at=now,
            evidence_hash=canonical_sha256(
                {
                    "request": request.to_mapping(),
                    "pull_number": pull_number,
                    "reviewer_login": reviewer_login,
                }
            ),
        )

    def _rev_parse(self, ref: str) -> str:
        try:
            return self._run_git(self._repo_root, "rev-parse", "--verify", ref)
        except subprocess.CalledProcessError as exc:
            raise SourceCandidateResolutionError(
                f"git rev-parse failed for {ref!r}: {exc.stderr.strip()}"
            ) from exc

    def _find_pull_request(
        self, request: SourceCandidateVerificationRequest
    ) -> tuple[int, str]:
        source_commit = request.source_commit
        all_pulls = self._fetch_pull_requests(
            f"repos/{self._repository}/commits/{source_commit}/pulls"
        )
        # A commit can legitimately appear in more than one PR's history
        # (cherry-pick, rebase onto another branch, a previously-merged PR
        # whose exact commit later becomes the pilot tip). Only a PR that
        # actually targets THIS promotion -- this candidate_ref as head,
        # this base_ref as base, still open -- certifies this operation;
        # anything else is an unrelated/closed/different-base PR whose
        # approvals were never given for the promotion being verified.
        pulls = [
            pull
            for pull in all_pulls
            if pull.get("state") == "open"
            and pull.get("head", {}).get("ref") == request.candidate_ref
            and pull.get("base", {}).get("ref") == request.base_ref
        ]
        if not pulls:
            raise SourceCandidateResolutionError(
                f"no open pull request with head={request.candidate_ref!r} "
                f"base={request.base_ref!r} found for commit {source_commit} "
                f"({len(all_pulls)} unrelated pull request(s) also reference "
                "this commit)"
            )
        # Multiple matching PRs would still be ambiguous -- refuse rather
        # than guess which one is authoritative.
        if len(pulls) > 1:
            raise SourceCandidateResolutionError(
                f"commit {source_commit} is associated with {len(pulls)} open "
                f"pull requests matching head={request.candidate_ref!r} "
                f"base={request.base_ref!r}, expected exactly one"
            )
        pull = pulls[0]
        return pull["number"], pull["user"]["login"]

    def _find_qualifying_review(
        self, pull_number: int, author_login: str, source_commit: str
    ) -> str:
        reviews = self._fetch_reviews(
            f"repos/{self._repository}/pulls/{pull_number}/reviews"
        )
        for review in reviews:
            if review.get("state") != "APPROVED":
                continue
            if review.get("commit_id") != source_commit:
                continue
            reviewer_login = review["user"]["login"]
            if reviewer_login == author_login:
                continue
            if reviewer_login in {VERIFIER_IDENTITY, CHECK_SUITE_VERIFIER_IDENTITY}:
                continue
            return reviewer_login
        raise SourceCandidateResolutionError(
            f"no independent APPROVED review found for commit {source_commit} "
            f"on PR #{pull_number} (reviews must target this exact commit, "
            "not a later or earlier one)"
        )

    @staticmethod
    def _receipt_id(request: SourceCandidateVerificationRequest) -> str:
        return str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"source-candidate:{request.request_digest}")
        )
