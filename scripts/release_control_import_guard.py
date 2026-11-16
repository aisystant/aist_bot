#!/usr/bin/env python3
"""Fail when release-control files reach a branch that does not enforce the gate.

The release-control contour (ledger, authority, resolvers, behaviour scenarios)
is developed on `pilot`. A branch that Railway builds must not carry those files
while the mandatory promotion gate is still switched off: a half-wired gate looks
like a working one, and every push to such a branch starts a build whose contour
code nobody promised to keep inert.

Which paths are gated is declared explicitly in `.github/release-control-contour.json`,
not guessed from file names: a name pattern both missed real contour files and
flagged this guard's own workflow. A branch opts in through
`.github/release-control-enforcement.json`, and the opt-in only counts when the
release contract itself has the cutover enabled - otherwise one line of JSON
would switch the guard off while the gate stays dormant.

The newer fast-forward release path is accepted separately on `new-architecture`:
the legacy cutover must remain disabled, the old enforcement marker must be absent,
and both the contour declaration and the release workflow must match their reviewed
versions byte-for-byte. Any edit to either file fails closed until this guard is
reviewed and its pins are updated.

Known limit: the declared list goes stale if a new contour file lands on `pilot`
without being added here. That is a review duty, not something this script can see.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

CONTOUR_FILE = ".github/release-control-contour.json"
ENFORCEMENT_FILE = ".github/release-control-enforcement.json"
CONTRACT_FILE = ".github/release-control-contract.json"
FAST_FORWARD_BRANCH = "new-architecture"
FAST_FORWARD_WORKFLOW = ".github/workflows/release-promote.yml"
FAST_FORWARD_CONTOUR_SHA256 = (
    "0d116092cee695b8bf33646918f11ebe24f9b366536dd17fba3f379240ff47b2"
)
FAST_FORWARD_WORKFLOW_SHA256 = (
    "af4e1709b8d9d1ca32b82c30c73121bbea2498f148379c15f48b5032a87b0dc4"
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _tracked_blob(repo_root: Path, path: str, commit: str = "HEAD") -> bytes | None:
    tree = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", commit, "--", path],
        check=False,
        capture_output=True,
        text=True,
    )
    if tree.returncode != 0:
        return None
    listing = tree.stdout.splitlines()
    if len(listing) != 1:
        return None
    mode, object_type, oid, listed_path = listing[0].split(None, 3)
    if listed_path != path or mode not in {"100644", "100755"} or object_type != "blob":
        return None
    blob = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "blob", oid],
        check=False,
        capture_output=True,
    )
    return blob.stdout if blob.returncode == 0 else None


def _tracked_entry_exists(repo_root: Path, path: str, commit: str = "HEAD") -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", f"{commit}:{path}"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _matches_pin(
    repo_root: Path, path: str, expected_sha256: str, commit: str = "HEAD"
) -> bool:
    blob = _tracked_blob(repo_root, path, commit)
    return blob is not None and hashlib.sha256(blob).hexdigest() == expected_sha256


def read_gated_paths(repo_root: Path) -> list[str]:
    declared = _load_json(repo_root / CONTOUR_FILE)
    paths = declared.get("gated_paths")
    if (
        not isinstance(paths, list)
        or not paths
        or any(not isinstance(p, str) for p in paths)
    ):
        raise ValueError(
            f"{CONTOUR_FILE}: gated_paths must be a non-empty list of strings"
        )
    return paths


def branch_enforces(repo_root: Path, branch: str) -> bool:
    """True only when the branch opted in AND the contract cutover is enabled."""
    marker = repo_root / ENFORCEMENT_FILE
    if not marker.exists():
        return False
    branches = _load_json(marker).get("enforced_branches", [])
    if not isinstance(branches, list) or any(not isinstance(b, str) for b in branches):
        raise ValueError(
            f"{ENFORCEMENT_FILE}: enforced_branches must be a list of strings"
        )
    if branch not in branches:
        return False

    contract = repo_root / CONTRACT_FILE
    if not contract.exists():
        raise ValueError(
            f"{ENFORCEMENT_FILE} enforces {branch} but {CONTRACT_FILE} is missing"
        )
    cutover = _load_json(contract).get("cutover", {})
    if not isinstance(cutover, dict):
        raise TypeError(f"{CONTRACT_FILE}: cutover must be an object")
    return cutover.get("enabled") is True


def fast_forward_enforces(repo_root: Path, branch: str, commit: str = "HEAD") -> bool:
    if branch != FAST_FORWARD_BRANCH:
        return False
    if _tracked_entry_exists(repo_root, ENFORCEMENT_FILE, commit):
        return False
    if not (
        _matches_pin(repo_root, CONTOUR_FILE, FAST_FORWARD_CONTOUR_SHA256, commit)
        and _matches_pin(
            repo_root, FAST_FORWARD_WORKFLOW, FAST_FORWARD_WORKFLOW_SHA256, commit
        )
    ):
        return False
    contract = _tracked_blob(repo_root, CONTRACT_FILE, commit)
    if contract is None:
        return False
    cutover = json.loads(contract.decode("utf-8")).get("cutover", {})
    if not isinstance(cutover, dict):
        raise TypeError(f"{CONTRACT_FILE}: cutover must be an object")
    return cutover.get("enabled") is False


def find_violations(
    tracked_paths: list[str], gated_paths: list[str], enforced: bool
) -> list[str]:
    if enforced:
        return []
    tracked = set(tracked_paths)
    return sorted(path for path in gated_paths if path in tracked)


def repo_root_of(start: Path) -> Path:
    """Resolve the repository root, so running from a subdirectory cannot pass silently."""
    out = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(out)


def tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release-control import guard")
    parser.add_argument(
        "--branch", required=True, help="branch under test, e.g. new-architecture"
    )
    parser.add_argument(
        "--repo-root", default=".", help="any path inside the repository (default: cwd)"
    )
    parser.add_argument(
        "--commit",
        default="HEAD",
        help="tree to inspect for the fast-forward pin (default: checked-out HEAD)",
    )
    args = parser.parse_args(argv)

    repo_root = repo_root_of(Path(args.repo_root).resolve())
    fast_forward = fast_forward_enforces(repo_root, args.branch, args.commit)
    violations = find_violations(
        tracked_files(repo_root),
        read_gated_paths(repo_root),
        branch_enforces(repo_root, args.branch) or fast_forward,
    )
    if not violations:
        if fast_forward:
            print(
                f"release-control import guard: OK for branch {args.branch} "
                "via pinned fast-forward gate; legacy cutover remains disabled"
            )
        else:
            print(f"release-control import guard: OK for branch {args.branch}")
        return 0

    print(
        f"release-control import guard: branch {args.branch} does not enforce the promotion gate, "
        f"but carries {len(violations)} contour file(s):",
        file=sys.stderr,
    )
    for path in violations:
        print(f"  {path}", file=sys.stderr)
    print(
        f"Enforce the gate for this branch in {ENFORCEMENT_FILE} and enable the cutover in "
        f"{CONTRACT_FILE} in the same change that wires it, or keep the contour on a branch "
        "that is not built.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
