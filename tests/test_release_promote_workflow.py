"""WP-562: the release workflow only fast-forwards production to a pilot commit.

The candidate check is executed for real against a throwaway origin, so the
test observes the same shell the workflow runs, not a description of it.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release-promote.yml"


def load_workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step(job, step_id):
    for item in load_workflow()["jobs"][job]["steps"]:
        if item.get("id") == step_id:
            return item
    raise AssertionError(f"step {step_id} not found in job {job}")


def clean_env():
    # A git hook may export GIT_DIR and friends; they would redirect every call.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=clean_env(),
    ).stdout.strip()


def commit(cwd, name):
    (Path(cwd) / name).write_text(name, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "seed"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True, env=clean_env()
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "pilot", str(work)], check=True, env=clean_env()
    )
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    base = commit(work, "base")
    git(work, "branch", "new-architecture", base)
    pilot_head = commit(work, "feature")
    git(work, "checkout", "-q", "-b", "prod-only", base)
    prod_only = commit(work, "hotfix")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "pilot", "new-architecture", "prod-only")
    runner = tmp_path / "runner"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(runner)], check=True, env=clean_env()
    )
    return {
        "runner": runner,
        "seed": work,
        "base": base,
        "pilot_head": pilot_head,
        "prod_only": prod_only,
    }


def run_check(runner, requested, tmp_path):
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    script = step("verify", "check")["run"]
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=runner,
        check=False,
        env={
            **clean_env(),
            "REQUESTED": requested,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        capture_output=True,
        text=True,
    )
    outputs = dict(
        line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
    )
    return result, outputs


def test_pilot_commit_ahead_of_production_is_accepted(clone, tmp_path):
    result, outputs = run_check(clone["runner"], clone["pilot_head"][:10], tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"sha": clone["pilot_head"], "previous": clone["base"]}
    summary = (tmp_path / "summary").read_text(encoding="utf-8")
    assert clone["pilot_head"] in summary
    assert "feature" in summary


@pytest.mark.parametrize("requested", ["origin/pilot", "HEAD", "abc", "--all"])
def test_non_hex_input_is_rejected(clone, tmp_path, requested):
    result, outputs = run_check(clone["runner"], requested, tmp_path)
    assert result.returncode == 1
    assert "must be a hex commit id" in result.stdout
    assert outputs == {}


def test_commit_outside_pilot_is_rejected(clone, tmp_path):
    result, outputs = run_check(clone["runner"], clone["prod_only"], tmp_path)
    assert result.returncode == 1
    assert "is not in pilot" in result.stdout
    assert outputs == {}


def test_diverged_production_is_rejected(clone, tmp_path):
    seed = clone["seed"]
    git(
        seed,
        "push",
        "-q",
        "origin",
        f"{clone['prod_only']}:refs/heads/new-architecture",
        "--force",
    )
    # The workflow checks out fresh with fetch-depth 0; mirror that state.
    git(clone["runner"], "fetch", "-q", "--force", "origin")
    result, outputs = run_check(clone["runner"], clone["pilot_head"], tmp_path)
    assert result.returncode == 1
    assert "fast-forward is impossible" in result.stdout
    assert outputs == {}


def test_release_of_current_production_is_rejected(clone, tmp_path):
    result, _ = run_check(clone["runner"], clone["base"], tmp_path)
    assert result.returncode == 1
    assert "already at" in result.stdout


def test_jobs_run_only_from_the_default_branch():
    jobs = load_workflow()["jobs"]
    for name in ("verify", "promote"):
        assert jobs[name]["if"] == "github.ref == 'refs/heads/new-architecture'"


def test_promotion_never_forces_and_waits_for_the_production_environment():
    promote = load_workflow()["jobs"]["promote"]
    assert promote["environment"] == "production"
    assert promote["needs"] == "verify"
    steps = {item["name"]: item for item in promote["steps"]}
    move = steps["Move new-architecture without force"]["run"]
    assert (
        "steps.token.outputs.token"
        in steps["Move new-architecture without force"]["env"]["GH_TOKEN"]
    )
    assert (
        "compare/${SHA}...pilot"
        in steps["Candidate is still in pilot after approval"]["run"]
    )
    assert "-F force=false" in move
    assert "refs/heads/new-architecture" in move
    assert "--force" not in move.replace("-F force=false", "")
