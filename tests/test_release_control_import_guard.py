"""Tests for the release-control import guard."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release_control_import_guard as guard

GATED = ["scripts/release_control.py", ".github/release-control-contract.json"]
UNRELATED = [
    "oauth_server.py",
    "db/queries/identity.py",
    "scripts/release-drift-detector.py",
]


def _repo(tmp_path: Path, tracked: list[str], contour: list[str] | None = None) -> Path:
    """Build a real git repository, so the guard is tested through git, not a stub."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".github").mkdir(exist_ok=True)
    (tmp_path / guard.CONTOUR_FILE).write_text(
        json.dumps({"gated_paths": contour if contour is not None else GATED}),
        encoding="utf-8",
    )
    for rel in tracked:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def test_declared_contour_path_blocks_an_unenforced_branch():
    assert guard.find_violations(GATED + UNRELATED, GATED, enforced=False) == sorted(
        GATED
    )


def test_enforcing_branch_accepts_the_whole_contour():
    assert guard.find_violations(GATED + UNRELATED, GATED, enforced=True) == []


def test_paths_outside_the_declared_list_are_never_violations():
    assert guard.find_violations(UNRELATED, GATED, enforced=False) == []


def test_the_guard_and_its_own_workflow_are_not_gated(tmp_path):
    """The first version flagged its own workflow and would have stayed red forever."""
    own = [
        "scripts/release_control_import_guard.py",
        "tests/test_release_control_import_guard.py",
        ".github/workflows/release-control-import-guard.yml",
    ]
    repo = _repo(tmp_path, own)
    assert (
        guard.find_violations(
            guard.tracked_files(repo), guard.read_gated_paths(repo), False
        )
        == []
    )


def test_empty_declared_list_fails_closed(tmp_path):
    repo = _repo(tmp_path, [], contour=[])
    with pytest.raises(ValueError):
        guard.read_gated_paths(repo)


def test_opt_in_without_contract_cutover_does_not_enforce(tmp_path):
    repo = _repo(tmp_path, GATED)
    (repo / guard.ENFORCEMENT_FILE).write_text(
        json.dumps({"enforced_branches": ["new-architecture"]}), encoding="utf-8"
    )
    (repo / guard.CONTRACT_FILE).write_text(
        json.dumps({"cutover": {"enabled": False}}), encoding="utf-8"
    )
    assert guard.branch_enforces(repo, "new-architecture") is False


def test_opt_in_with_contract_cutover_enforces(tmp_path):
    repo = _repo(tmp_path, GATED)
    (repo / guard.ENFORCEMENT_FILE).write_text(
        json.dumps({"enforced_branches": ["new-architecture"]}), encoding="utf-8"
    )
    (repo / guard.CONTRACT_FILE).write_text(
        json.dumps({"cutover": {"enabled": True}}), encoding="utf-8"
    )
    assert guard.branch_enforces(repo, "new-architecture") is True


def test_opt_in_for_another_branch_does_not_enforce_this_one(tmp_path):
    repo = _repo(tmp_path, GATED)
    (repo / guard.ENFORCEMENT_FILE).write_text(
        json.dumps({"enforced_branches": ["pilot"]}), encoding="utf-8"
    )
    (repo / guard.CONTRACT_FILE).write_text(
        json.dumps({"cutover": {"enabled": True}}), encoding="utf-8"
    )
    assert guard.branch_enforces(repo, "new-architecture") is False


def test_opt_in_without_contract_file_fails_closed(tmp_path):
    repo = _repo(tmp_path, GATED)
    (repo / guard.ENFORCEMENT_FILE).write_text(
        json.dumps({"enforced_branches": ["new-architecture"]}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        guard.branch_enforces(repo, "new-architecture")


def test_malformed_contract_cutover_fails_closed(tmp_path):
    repo = _repo(tmp_path, GATED)
    (repo / guard.ENFORCEMENT_FILE).write_text(
        json.dumps({"enforced_branches": ["new-architecture"]}), encoding="utf-8"
    )
    (repo / guard.CONTRACT_FILE).write_text(
        json.dumps({"cutover": "yes"}), encoding="utf-8"
    )
    with pytest.raises(TypeError):
        guard.branch_enforces(repo, "new-architecture")


def test_malformed_enforcement_marker_fails_closed(tmp_path):
    repo = _repo(tmp_path, GATED)
    (repo / guard.ENFORCEMENT_FILE).write_text(
        json.dumps({"enforced_branches": "new-architecture"}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        guard.branch_enforces(repo, "new-architecture")


def test_cli_rejects_contour_on_unenforced_branch(tmp_path, capsys):
    repo = _repo(tmp_path, GATED)
    assert guard.main(["--branch", "new-architecture", "--repo-root", str(repo)]) == 1
    captured = capsys.readouterr()
    assert "scripts/release_control.py" in captured.err


def test_cli_accepts_a_tree_without_contour(tmp_path, capsys):
    repo = _repo(tmp_path, UNRELATED)
    assert guard.main(["--branch", "new-architecture", "--repo-root", str(repo)]) == 0
    assert "OK for branch new-architecture" in capsys.readouterr().out


def test_cli_from_a_subdirectory_still_sees_the_whole_repository(tmp_path):
    """Running from a subdirectory used to list only that subtree and pass falsely."""
    repo = _repo(tmp_path, GATED)
    subdir = repo / "scripts"
    assert guard.main(["--branch", "new-architecture", "--repo-root", str(subdir)]) == 1
