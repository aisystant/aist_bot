"""Tests for .githooks/pre-push: Pilot-First checks must use the server's pilot.

WP-7 F310 (2026-11-16): a stale local `pilot` in a shared checkout produced a
false "92 prod commits missing from pilot" block on a clean fast-forward.
"""

import os
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / ".githooks"


def _git(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    result = _git(repo, "commit", "-q", "-m", f"add {name}")
    assert result.returncode == 0, result.stderr


@pytest.fixture
def repos(tmp_path: Path):
    """Server with pilot and prod on one base; a clone whose local pilot is stale."""
    server = tmp_path / "server.git"
    _git(tmp_path, "init", "-q", "--bare", str(server))

    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "pilot", str(seed))
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    _commit(seed, "base.txt")
    _git(seed, "remote", "add", "origin", str(server))
    _git(seed, "push", "-q", "origin", "pilot", "pilot:new-architecture")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "-b", "pilot", str(server), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    return seed, clone


def _new_prod_commit(clone: Path) -> str:
    """A commit prod does not have yet, so git actually runs the hook."""
    _commit(clone, "next.txt")
    return _git(clone, "rev-parse", "HEAD").stdout.strip()


def _push_prod(clone: Path, sha: str, force_prod: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "FORCE_PROD": "1" if force_prod else "0"}
    return _git(
        clone,
        "-c", f"core.hooksPath={HOOKS_DIR}",
        "push", "origin", f"{sha}:refs/heads/new-architecture",
        env=env,
    )


def test_fast_forward_passes_when_local_pilot_is_stale(repos):
    seed, clone = repos
    # Pilot moves on the server with 12 commits; the clone's local pilot stays old.
    for i in range(12):
        _commit(seed, f"pilot-{i}.txt")
    _git(seed, "push", "-q", "origin", "pilot")
    _git(clone, "fetch", "-q", "origin")
    server_pilot = _git(clone, "rev-parse", "origin/pilot").stdout.strip()
    stale_local = _git(clone, "rev-parse", "pilot").stdout.strip()
    assert server_pilot != stale_local

    result = _push_prod(clone, server_pilot)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PUSH BLOCKED" not in result.stdout + result.stderr
    prod = _git(clone, "ls-remote", "origin", "refs/heads/new-architecture").stdout.split()[0]
    assert prod == server_pilot


def test_real_drift_over_threshold_is_blocked(repos):
    _seed, clone = repos
    # 11 commits that never reached pilot on the server.
    _git(clone, "checkout", "-q", "-b", "hotfixes", "origin/new-architecture")
    for i in range(11):
        _commit(clone, f"prod-only-{i}.txt")
    head = _git(clone, "rev-parse", "HEAD").stdout.strip()

    result = _push_prod(clone, head)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "11 правок на new-architecture отсутствуют" in output


def test_push_is_blocked_when_server_pilot_cannot_be_fetched(repos):
    seed, clone = repos
    _git(seed, "push", "-q", "origin", "--delete", "pilot")
    head = _new_prod_commit(clone)

    result = _push_prod(clone, head)

    assert result.returncode != 0
    assert "не удалось получить ветку pilot" in result.stdout + result.stderr


def test_push_without_force_prod_is_blocked(repos):
    _seed, clone = repos
    head = _new_prod_commit(clone)

    result = _push_prod(clone, head, force_prod=False)

    assert result.returncode != 0
    assert "защищена (Pilot-First)" in result.stdout + result.stderr
