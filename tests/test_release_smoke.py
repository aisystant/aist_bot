"""Observe actual pytest subprocesses; malformed evidence must fail closed."""

from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import subprocess
import sys
import textwrap
import time
from copy import deepcopy
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts/run_release_smoke.py"
SPEC = importlib.util.spec_from_file_location("release_smoke_under_test", RUNNER_PATH)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


@pytest.fixture
def project(tmp_path):
    repository = tmp_path / "candidate"
    repository.mkdir()
    (repository / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    output = tmp_path / "evidence"
    identity = {
        "candidate_commit": "1" * 40,
        "ci_config_commit": "1" * 40,
        "profile": "fixture",
        "paths": ["test_sample.py"],
    }
    return repository, output, identity


def write_test(project, source, conftest=""):
    repository, _, _ = project
    (repository / "test_sample.py").write_text(
        textwrap.dedent(source), encoding="utf-8"
    )
    if conftest:
        (repository / "conftest.py").write_text(
            textwrap.dedent(conftest), encoding="utf-8"
        )


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_records_exact_executed_ids_and_candidate_profile(project):
    write_test(
        project,
        """
        import pytest
        @pytest.mark.parametrize('number', [1, 2])
        def test_double(number):
            assert number * 2 in (2, 4)
    """,
    )
    repository, output, identity = project
    result = smoke.run_smoke(repository, output, identity)
    assert result["status"] == "passed"
    assert result["executed_test_ids"] == [
        "test_sample.py::test_double[1]",
        "test_sample.py::test_double[2]",
    ]
    baseline = read_json(output / "allowed-test-ids.json")
    assert baseline == {**identity, "test_ids": result["executed_test_ids"]}
    assert result["allowed_test_ids_digest"] == smoke._sha256(baseline)
    assert result["timeout_policy_seconds"] == 120
    assert result["review_after_seconds"] == 60
    assert len(read_json(output / "run.json")["reports"]) == 6


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\n@pytest.mark.skip(reason='offline')\ndef test_example(): pass",
        "import pytest\ndef test_example(): pytest.skip('offline')",
        "import pytest\n@pytest.mark.xfail\ndef test_example(): assert False",
        "import pytest\n@pytest.mark.xfail\ndef test_example(): assert 2 + 2 == 4",
        "def test_example(): assert 2 + 2 == 5",
        (
            "import pytest\n@pytest.fixture\ndef broken(): raise RuntimeError('fixture')\n"
            "def test_example(broken): pass"
        ),
        (
            "import pytest\n@pytest.fixture\ndef broken():\n yield\n raise RuntimeError('teardown')\n"
            "def test_example(broken): assert 2 + 2 == 4"
        ),
    ],
)
def test_rejects_every_non_successful_test_phase(project, source):
    write_test(project, source)
    with pytest.raises(smoke.SmokeRejected):
        smoke.run_smoke(*project)
    assert read_json(project[1] / "result.json")["status"] == "rejected"


@pytest.mark.parametrize(
    "source",
    [
        "def broken syntax",
        "raise RuntimeError('collection failed')",
        "import pytest\npytest.skip('module skipped', allow_module_level=True)",
        "# zero collected tests",
        "import os\nos._exit(0)",
    ],
)
def test_collection_failure_skip_empty_or_missing_receipt_blocks_run(project, source):
    write_test(project, source)
    with pytest.raises(smoke.SmokeRejected):
        smoke.run_smoke(*project)
    assert not (project[1] / "run.json").exists()
    assert read_json(project[1] / "result.json")["status"] == "rejected"


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "filtered"])
def test_run_collection_must_equal_independent_baseline(project, change):
    mutation = {
        "missing": "items.pop()",
        "extra": "items[-1]._nodeid += '-unexpected'",
        "duplicate": "items.append(items[-1])",
        "filtered": "config.hook.pytest_deselected(items=[items.pop()])",
    }[change]
    write_test(
        project,
        """
        def test_one(): assert 2 + 2 == 4
        def test_two(): assert 'smoke'.upper() == 'SMOKE'
    """,
        f"""
        def pytest_collection_modifyitems(config, items):
            if not config.option.collectonly:
                {mutation}
    """,
    )
    with pytest.raises(smoke.SmokeRejected):
        smoke.run_smoke(*project)
    assert len(read_json(project[1] / "allowed-test-ids.json")["test_ids"]) == 2
    assert read_json(project[1] / "result.json")["status"] == "rejected"


def test_duplicate_baseline_is_rejected(project):
    write_test(
        project,
        "def test_one(): assert 2 + 2 == 4",
        """
        def pytest_collection_modifyitems(items):
            items.append(items[0])
    """,
    )
    with pytest.raises(smoke.SmokeRejected, match="duplicate"):
        smoke.run_smoke(*project)
    assert not (project[1] / "run.json").exists()


def test_collected_but_not_executed_test_does_not_pass(project):
    write_test(
        project,
        "def test_one(): assert 2 + 2 == 4",
        """
        def pytest_runtest_protocol(item, nextitem):
            return True
    """,
    )
    with pytest.raises(smoke.SmokeRejected, match="missing"):
        smoke.run_smoke(*project)
    execution = read_json(project[1] / "run.json")
    assert execution["exit_code"] == 0
    assert execution["reports"] == []


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "unknown", "skipped"])
def test_independent_phase_receipt_validation_rejects_false_green(project, mutation):
    write_test(project, "def test_one(): assert 2 + 2 == 4")
    result = smoke.run_smoke(*project)
    receipt = read_json(project[1] / "run.json")
    if mutation == "duplicate":
        receipt["reports"].append(deepcopy(receipt["reports"][0]))
    elif mutation == "missing":
        receipt["reports"].pop()
    elif mutation == "unknown":
        receipt["reports"][0]["nodeid"] = "test_sample.py::unknown"
    else:
        receipt["reports"][0]["outcome"] = "skipped"
    with pytest.raises(smoke.SmokeRejected):
        smoke.verify_execution(result["executed_test_ids"], receipt)


def test_timeout_stops_worker_and_emits_review_signal(project, capsys):
    write_test(
        project,
        """
        import time
        def test_slow():
            time.sleep(10)
            raise AssertionError('deadline was not enforced')
    """,
    )
    repository, output, identity = project
    output.mkdir()
    request = output / "request.json"
    smoke._write_json(request, identity)
    started = time.monotonic()
    with pytest.raises(smoke.SmokeRejected, match="timeout_policy"):
        smoke.run_phase(
            repository, request, "run", timeout_seconds=0.8, review_after_seconds=0.4
        )
    assert time.monotonic() - started < 3
    assert "::warning::" in capsys.readouterr().out
    assert not (output / "run.json").exists()
    assert smoke.TIMEOUT_SECONDS == 120


def test_slow_success_signals_review_without_changing_policy(project, capsys):
    write_test(
        project,
        """
        import time
        def test_slow():
            time.sleep(0.3)
            assert 'test'.startswith('t')
    """,
    )
    repository, output, identity = project
    output.mkdir()
    request = output / "request.json"
    smoke._write_json(request, identity)
    receipt = smoke.run_phase(
        repository,
        request,
        "run",
        timeout_seconds=4,
        review_after_seconds=0.1,
    )
    assert receipt["process_exit_code"] == 0
    assert receipt["timeout_review_required"] is True
    assert "timeout remains unchanged" in capsys.readouterr().out


def test_worker_excludes_credentials_and_pytest_filters(project, monkeypatch):
    monkeypatch.setenv("RELEASE_SMOKE_SECRET_FIXTURE", "must-not-reach-child")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nonexistent")
    write_test(
        project,
        """
        import os
        def test_environment():
            assert 'RELEASE_SMOKE_SECRET_FIXTURE' not in os.environ
            assert 'PYTEST_ADDOPTS' not in os.environ
            assert os.environ['DATABASE_URL'] == 'postgresql://fake:fake@localhost:5432/fake'
    """,
    )
    assert smoke.run_smoke(*project)["status"] == "passed"


def test_worker_blocks_network_and_environment_file_reads(project):
    write_test(
        project,
        """
        import pathlib
        import socket
        import pytest
        def test_offline():
            with pytest.raises(PermissionError, match='network'):
                socket.getaddrinfo('example.invalid', 443)
            with pytest.raises(PermissionError, match='environment'):
                pathlib.Path('.env').read_text()
    """,
    )
    assert smoke.run_smoke(*project)["status"] == "passed"


def test_worker_does_not_load_ignored_bytecode_instead_of_candidate_source(project):
    repository, _, _ = project
    (repository / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\n", encoding="utf-8"
    )
    helper = repository / "helper.py"
    helper.write_text("ANSWER = 1\n", encoding="utf-8")
    poison = repository / "poison.py"
    poison.write_text("ANSWER = 0\n", encoding="utf-8")
    source_time = helper.stat().st_mtime_ns
    os.utime(poison, ns=(source_time, source_time))
    cached = Path(importlib.util.cache_from_source(str(helper)))
    cached.parent.mkdir()
    py_compile.compile(str(poison), cfile=str(cached), dfile=str(helper), doraise=True)
    write_test(
        project,
        """
        import helper
        def test_source():
            assert helper.ANSWER == 1
    """,
    )
    assert smoke.run_smoke(*project)["status"] == "passed"


def test_existing_output_directory_cannot_reuse_stale_evidence(project):
    write_test(project, "def test_one(): assert 2 + 2 == 4")
    smoke.run_smoke(*project)
    with pytest.raises(FileExistsError):
        smoke.run_smoke(*project)


def test_cli_does_not_accept_a_floating_ref_or_unknown_profile(project):
    repository, output, _ = project
    with pytest.raises(smoke.SmokeRejected, match="immutable"):
        smoke.verify_candidate(repository, "pilot")
    with pytest.raises(SystemExit) as rejected:
        smoke.main(
            [
                "--candidate-commit",
                "1" * 40,
                "--profile",
                "empty",
                "--output-dir",
                str(output),
            ]
        )
    assert rejected.value.code == 2


def test_cli_rejects_commit_mismatch_and_dirty_tree(project, monkeypatch):
    repository, _, _ = project
    monkeypatch.setattr(smoke, "_git", lambda *_args: "2" * 40)
    with pytest.raises(smoke.SmokeRejected, match="HEAD"):
        smoke.verify_candidate(repository, "1" * 40)

    def dirty_git(_repo, *arguments):
        return "1" * 40 if arguments[0] == "rev-parse" else " M test_sample.py"

    monkeypatch.setattr(smoke, "_git", dirty_git)
    with pytest.raises(smoke.SmokeRejected, match="clean"):
        smoke.verify_candidate(repository, "1" * 40)


@pytest.mark.parametrize(
    "failure", [OSError("unavailable"), subprocess.TimeoutExpired("git", 15)]
)
def test_git_verification_failure_is_a_rejected_candidate(
    project, monkeypatch, failure
):
    def unavailable(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(smoke.subprocess, "run", unavailable)
    with pytest.raises(smoke.SmokeRejected, match="Git identity"):
        smoke.verify_candidate(project[0], "1" * 40)


def test_cli_binds_a_real_clean_candidate_and_rejects_later_source_changes(tmp_path):
    repository = tmp_path / "candidate"
    repository.mkdir()
    runner = repository / "scripts/run_release_smoke.py"
    runner.parent.mkdir()
    runner.write_bytes(RUNNER_PATH.read_bytes())
    workflow = repository / smoke.WORKFLOW_PATH
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: fixture\n", encoding="utf-8")
    tracked = ["scripts/run_release_smoke.py", smoke.WORKFLOW_PATH]
    for relative in smoke.PROFILE_PATHS:
        path = repository / relative
        if relative.endswith("/"):
            path = path / "test_fixture.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_fixture(): assert 2 + 2 == 4\n", encoding="utf-8")
        tracked.append(path.relative_to(repository).as_posix())
    environment = smoke._test_environment()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})

    def git(*arguments):
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Smoke fixture",
                "-c",
                "user.email=smoke@example.invalid",
                *arguments,
            ],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()

    git("init", "-q")
    git("add", "--", *tracked)
    git("commit", "-q", "-m", "test fixture")
    candidate = git("rev-parse", "HEAD")
    output = tmp_path / "evidence"
    command = [
        sys.executable,
        "-I",
        "-B",
        str(runner),
        "--candidate-commit",
        candidate,
        "--profile",
        smoke.PROFILE,
        "--output-dir",
        str(output),
    ]
    result = subprocess.run(
        command,
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    receipt = read_json(output / "result.json")
    assert receipt["candidate_commit"] == candidate
    assert receipt["ci_config_commit"] == candidate
    assert len(receipt["executed_test_ids"]) == 4
    (repository / smoke.PROFILE_PATHS[1]).write_text("# changed\n", encoding="utf-8")
    command[-1] = str(tmp_path / "changed-evidence")
    rejected = subprocess.run(
        command,
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert rejected.returncode == 1
    assert "worktree is not clean" in rejected.stderr
    assert not (tmp_path / "changed-evidence").exists()
