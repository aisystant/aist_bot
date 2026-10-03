#!/usr/bin/env python3
"""Run the P-18 smoke profile and prove which tests actually completed.

Collection and execution use separate pytest processes. Their hook receipts,
never terminal counts, are compared against the exact candidate's profile.
This is offline test evidence, not deployment identity or production approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

TIMEOUT_SECONDS = 120
REVIEW_AFTER_SECONDS = 60
PROFILE = "l1-l2"
PROFILE_PATHS = (
    "tests/smoke/",
    "tests/test_search_path_independence.py",
    "tests/test_db_pool_settings.py",
    "tests/test_readiness.py",
)
WORKFLOW_PATH = ".github/workflows/smoke-tests.yml"


class SmokeRejected(ValueError):
    """Missing or inconsistent smoke evidence must block the release check."""


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _sha256(payload: Any) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SmokeRejected("cannot verify the candidate Git identity") from exc
    if result.returncode:
        raise SmokeRejected("cannot verify the candidate Git identity")
    return result.stdout.strip()


def verify_candidate(repository: Path, candidate_commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", candidate_commit):
        raise SmokeRejected("candidate must be a full immutable Git commit")
    if _git(repository, "rev-parse", "HEAD") != candidate_commit:
        raise SmokeRejected("candidate commit differs from checked-out HEAD")
    if _git(repository, "status", "--porcelain", "--untracked-files=normal"):
        raise SmokeRejected("candidate worktree is not clean")


def _test_environment() -> dict[str, str]:
    # No deployment credentials or caller-supplied pytest selection enter tests.
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
        if name in os.environ
    }
    environment.update(
        {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "TELEGRAM_BOT_TOKEN": "000000000:AAFakeTokenForTests",
            "ANTHROPIC_API_KEY": "sk-ant-fake-test-key",
            "DATABASE_URL": "postgresql://fake:fake@localhost:5432/fake",
            "KNOWLEDGE_MCP_URL": "https://fake-mcp.test/mcp",
            "USE_STATE_MACHINE": "false",
        }
    )
    return environment


def _offline_audit(event: str, arguments: tuple[Any, ...]) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise PermissionError("release smoke forbids network access")
    if event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(arguments[0]))
        if any(part == ".secrets" or part.startswith(".env") for part in path.parts):
            raise PermissionError("release smoke forbids reading environment files")


class SmokeObserver:
    """Record collection and every test phase, including skips and duplicates."""

    def __init__(self) -> None:
        self.receipt: dict[str, Any] = {
            "finished": False,
            "exit_code": None,
            "collected": [],
            "collection_problems": [],
            "deselected": [],
            "reports": [],
        }

    def pytest_collection_finish(self, session: Any) -> None:
        self.receipt["collected"] = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report: Any) -> None:
        if report.outcome != "passed":
            self.receipt["collection_problems"].append(
                {
                    "nodeid": report.nodeid,
                    "outcome": report.outcome,
                }
            )

    def pytest_deselected(self, items: list[Any]) -> None:
        self.receipt["deselected"].extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.receipt["reports"].append(
            {
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "wasxfail": hasattr(report, "wasxfail"),
            }
        )

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        self.receipt["finished"] = True
        self.receipt["exit_code"] = int(exitstatus)


def _worker(request_path: Path, phase: str) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    output = request_path.parent
    sys.addaudithook(_offline_audit)
    import pytest

    # -B blocks writes, but still reads ignored __pycache__ beside candidate code.
    sys.pycache_prefix = str(output / f"{phase}-bytecode")
    observer = SmokeObserver()
    arguments = [
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_asyncio.plugin",
        "--import-mode=importlib",
        "-o",
        "addopts=",
        "-q",
        "--tb=short",
        *request["paths"],
    ]
    if phase == "collect":
        arguments.append("--collect-only")
    else:
        arguments.append(f"--junitxml={output / 'smoke-results.xml'}")
    try:
        return int(pytest.main(arguments, plugins=[observer]))
    finally:
        _write_json(output / f"{phase}.json", observer.receipt)


def _stop_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # The child can exit between the timeout and process-group lookup.
        print("release smoke process had already stopped", file=sys.stderr)
    process.wait()


def run_phase(
    repository: Path,
    request_path: Path,
    phase: str,
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
    review_after_seconds: float = REVIEW_AFTER_SECONDS,
) -> dict[str, Any]:
    """Bound the entire child process, including collection and fixture teardown."""
    output = request_path.parent
    started = time.monotonic()
    review_required = False
    with (output / f"{phase}.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).resolve()),
                "_worker",
                str(request_path),
                phase,
            ],
            cwd=repository,
            env=_test_environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            try:
                process.wait(timeout=min(review_after_seconds, timeout_seconds))
            except subprocess.TimeoutExpired:
                review_required = True
                print(
                    f"::warning::P-18 {phase} reached 50% of timeout_policy; "
                    "pilot review required, timeout remains unchanged.",
                    flush=True,
                )
                remaining = timeout_seconds - (time.monotonic() - started)
                process.wait(timeout=max(remaining, 0))
        except subprocess.TimeoutExpired as exc:
            _stop_process_group(process)
            raise SmokeRejected(f"{phase} exceeded timeout_policy") from exc
        except BaseException:
            _stop_process_group(process)
            raise
    duration = time.monotonic() - started
    if duration >= timeout_seconds:
        raise SmokeRejected(f"{phase} exceeded timeout_policy")
    if duration >= review_after_seconds and not review_required:
        review_required = True
        print(
            f"::warning::P-18 {phase} reached 50% of timeout_policy; "
            "pilot review required, timeout remains unchanged.",
            flush=True,
        )
    try:
        receipt = json.loads((output / f"{phase}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SmokeRejected(f"{phase} did not produce a readable hook receipt") from exc
    if not isinstance(receipt, dict):
        raise SmokeRejected(f"{phase} produced a malformed hook receipt")
    receipt.update(
        {
            "process_exit_code": process.returncode,
            "duration_seconds": duration,
            "timeout_review_required": review_required,
        }
    )
    _write_json(output / f"{phase}.json", receipt)
    return receipt


def _collected_ids(receipt: dict[str, Any], phase: str) -> list[str]:
    if receipt.get("finished") is not True or receipt.get("exit_code") != 0:
        raise SmokeRejected(f"{phase} did not finish successfully")
    if receipt.get("process_exit_code") != 0:
        raise SmokeRejected(f"{phase} process failed")
    if receipt.get("collection_problems") != [] or receipt.get("deselected") != []:
        raise SmokeRejected(f"{phase} contains collection failures or filtered tests")
    ids = receipt.get("collected")
    if (
        not isinstance(ids, list)
        or not ids
        or not all(isinstance(x, str) and x for x in ids)
    ):
        raise SmokeRejected(f"{phase} has no valid collected test IDs")
    if len(ids) != len(set(ids)):
        raise SmokeRejected(f"{phase} contains duplicate test IDs")
    return ids


def verify_execution(expected: list[str], receipt: dict[str, Any]) -> list[str]:
    actual_collection = _collected_ids(receipt, "run")
    if set(expected) != set(actual_collection):
        raise SmokeRejected("run collection differs from the allowed test-ID set")
    reports = receipt.get("reports")
    if not isinstance(reports, list):
        raise SmokeRejected("run has no test-phase reports")
    observed: Counter[tuple[str, str]] = Counter()
    for report in reports:
        if not isinstance(report, dict) or set(report) != {
            "nodeid",
            "when",
            "outcome",
            "wasxfail",
        }:
            raise SmokeRejected("run contains a malformed test-phase report")
        nodeid, phase = report["nodeid"], report["when"]
        if nodeid not in expected or phase not in {"setup", "call", "teardown"}:
            raise SmokeRejected("run contains an unknown test ID or phase")
        if report["outcome"] != "passed" or report["wasxfail"] is not False:
            raise SmokeRejected("run contains a skipped, failed or xfailed test phase")
        observed[nodeid, phase] += 1
    required = Counter(
        (nodeid, phase)
        for nodeid in expected
        for phase in ("setup", "call", "teardown")
    )
    if observed != required:
        raise SmokeRejected("run contains missing or duplicate test-phase reports")
    return sorted(expected)


def run_smoke(
    repository: Path, output: Path, identity: dict[str, Any]
) -> dict[str, Any]:
    """Orchestrate offline evidence; the CLI verifies Git identity before/after."""
    output.mkdir(parents=True, exist_ok=False)
    request_path = output / "request.json"
    _write_json(request_path, identity)
    verdict: dict[str, Any] = {
        "schema_version": 1,
        **identity,
        "status": "rejected",
        "timeout_policy_seconds": TIMEOUT_SECONDS,
        "review_after_seconds": REVIEW_AFTER_SECONDS,
    }
    try:
        collection = run_phase(repository, request_path, "collect")
        allowed = sorted(_collected_ids(collection, "collect"))
        baseline = {**identity, "test_ids": allowed}
        _write_json(output / "allowed-test-ids.json", baseline)
        execution = run_phase(repository, request_path, "run")
        executed = verify_execution(allowed, execution)
        verdict.update(
            {
                "status": "passed",
                "executed_test_ids": executed,
                "allowed_test_ids_digest": _sha256(baseline),
                "duration_seconds": execution["duration_seconds"],
                "timeout_review_required": execution["timeout_review_required"],
            }
        )
    except SmokeRejected as exc:
        verdict["reason"] = str(exc)
        raise
    finally:
        _write_json(output / "result.json", verdict)
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--profile", choices=[PROFILE], required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    repository = Path.cwd().resolve()
    output = arguments.output_dir.resolve()
    try:
        if output == repository or repository in output.parents:
            raise SmokeRejected(
                "evidence directory must be outside the candidate worktree"
            )
        verify_candidate(repository, arguments.candidate_commit)
        runner_path = Path(__file__).resolve().relative_to(repository).as_posix()
        for required_path in (runner_path, WORKFLOW_PATH, *PROFILE_PATHS):
            if not _git(repository, "ls-files", "--", required_path):
                raise SmokeRejected(
                    "smoke runner and profile must belong to the candidate commit"
                )
        identity = {
            "candidate_commit": arguments.candidate_commit,
            "profile": arguments.profile,
            "paths": list(PROFILE_PATHS),
            "ci_config_commit": arguments.candidate_commit,
            "workflow": WORKFLOW_PATH,
            "runner_digest": "sha256:"
            + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        verdict = run_smoke(repository, output, identity)
        try:
            verify_candidate(repository, arguments.candidate_commit)
        except SmokeRejected as exc:
            verdict.update({"status": "rejected", "reason": str(exc)})
            _write_json(output / "result.json", verdict)
            raise
    except (SmokeRejected, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"::error::Release smoke rejected: {exc}", file=sys.stderr)
        return 1
    print(f"Release smoke passed: {len(verdict['executed_test_ids'])} exact test IDs")
    return 0


if __name__ == "__main__":
    if (
        len(sys.argv) == 4
        and sys.argv[1] == "_worker"
        and sys.argv[3] in {"collect", "run"}
    ):
        raise SystemExit(_worker(Path(sys.argv[2]), sys.argv[3]))
    raise SystemExit(main())
