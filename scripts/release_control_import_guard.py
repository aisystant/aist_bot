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

Known limit: the declared list goes stale if a new contour file lands on `pilot`
without being added here. That is a review duty, not something this script can see.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

CONTOUR_FILE = ".github/release-control-contour.json"
ENFORCEMENT_FILE = ".github/release-control-enforcement.json"
CONTRACT_FILE = ".github/release-control-contract.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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
    args = parser.parse_args(argv)

    repo_root = repo_root_of(Path(args.repo_root).resolve())
    violations = find_violations(
        tracked_files(repo_root),
        read_gated_paths(repo_root),
        branch_enforces(repo_root, args.branch),
    )
    if not violations:
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
