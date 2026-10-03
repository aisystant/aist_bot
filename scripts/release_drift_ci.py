#!/usr/bin/env python3
"""Carry the drift journal across trusted, serialized GitHub Actions runs.

The immediately preceding executed run is authoritative, including failed
runs. Missing evidence blocks observation rather than restarting the SLA.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ARTIFACT = "release-drift-state"
JOURNAL = "release-drift-manifest.jsonl"
METADATA = "release-drift-state.json"
MAX_BYTES = 32 * 1024 * 1024
TRUSTED_BRANCH = "new-architecture"
EVENTS = {"push", "workflow_run", "schedule", "workflow_dispatch"}


class StateError(RuntimeError):
    """The previous durable observation cannot be authenticated or recovered."""


def api(path: str, *, binary: bool = False):
    try:
        result = subprocess.run(
            ["gh", "api", path], capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateError("GitHub evidence API unavailable") from exc
    if result.returncode:
        # stderr can contain URLs or credentials supplied by the host.
        raise StateError("GitHub evidence API request failed")
    if len(result.stdout) > MAX_BYTES:
        raise StateError("GitHub evidence exceeded size limit")
    if binary:
        return result.stdout
    try:
        return json.loads(result.stdout)
    except (UnicodeError, ValueError) as exc:
        raise StateError("Malformed GitHub evidence") from exc


def context() -> tuple[str, int]:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise StateError("Invalid repository identity")
    if not run_id.isdecimal() or int(run_id) <= 0:
        raise StateError("Invalid run identity")
    if os.environ.get("GITHUB_RUN_ATTEMPT") != "1":
        raise StateError(
            "Start a new workflow dispatch; do not rerun an old observation"
        )
    return repository, int(run_id)


def preceding_run(runs: list[dict], current: dict, repository: str) -> dict | None:
    eligible = []
    for run in runs:
        if run["id"] == current["id"]:
            continue
        if run["head_branch"] != TRUSTED_BRANCH or run["event"] not in EVENTS:
            continue
        if (run.get("head_repository") or {}).get("full_name") != repository:
            continue
        if run.get("workflow_id") != current["workflow_id"]:
            continue
        if run.get("conclusion") == "skipped":
            continue
        # Cancelled/failed runs must have evidence or fail closed.
        if run["status"] in {"queued", "pending", "waiting", "requested"}:
            continue
        if run["status"] != "completed":
            raise StateError("Another observation has not completed")
        eligible.append(run)
    latest = max(eligible, key=lambda run: run["run_number"], default=None)
    if latest and latest["run_number"] > current["run_number"]:
        raise StateError(
            "A newer run already completed; an old run cannot rewind the journal"
        )
    return latest


def unpack_state(archive: bytes, *, repository: str, run_id: int) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            names = bundle.namelist()
            if sorted(names) != sorted([JOURNAL, METADATA]):
                raise StateError("Unexpected or duplicate state archive members")
            if sum(info.file_size for info in bundle.infolist()) > MAX_BYTES:
                raise StateError("State archive exceeds size limit")
            journal = bundle.read(JOURNAL)
            metadata = json.loads(bundle.read(METADATA))
    except (zipfile.BadZipFile, UnicodeError, ValueError, RuntimeError) as exc:
        raise StateError("Invalid state archive") from exc
    expected = {
        "schema_version": 1,
        "repository": repository,
        "run_id": run_id,
        "sha256": hashlib.sha256(journal).hexdigest(),
    }
    if metadata != expected:
        raise StateError("State provenance or journal checksum mismatch")
    return journal


def previous_observation(prefix: str, current: dict, repository: str) -> dict | None:
    runs = []
    for page in range(1, 11):
        batch = api(
            f"{prefix}/workflows/{current['workflow_id']}/runs?branch={TRUSTED_BRANCH}&per_page=100&page={page}"
        )["workflow_runs"]
        runs.extend(batch)
        while (prior := preceding_run(runs, current, repository)) is not None:
            if prior.get("run_attempt", 1) != 1:
                raise StateError(
                    "Prior observation was rerun; manual recovery is required"
                )
            # GitHub can replace a queued run even with cancel-in-progress
            # disabled. Only API proof that no job existed permits skipping it.
            jobs = api(f"{prefix}/runs/{prior['id']}/jobs?per_page=100")
            never_started = (
                jobs["total_count"] == 0 and prior.get("conclusion") == "cancelled"
            )
            entries = jobs.get("jobs", [])
            activation_skipped = (
                jobs["total_count"] == 1
                and len(entries) == 1
                and entries[0].get("name") == "Release drift observation"
                and entries[0].get("conclusion") == "skipped"
                and not entries[0].get("steps")
            )
            if never_started or activation_skipped:
                prior["conclusion"] = "skipped"
                continue
            return prior
        if len(batch) < 100:
            return None
    raise StateError("Workflow history window exhausted")


def validate_journal(path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "release_drift_detector", Path(__file__).with_name("release-drift-detector.py")
    )
    detector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(detector)
    try:
        detector.load_manifest(path)
    except detector.DetectorError as exc:
        raise StateError("Cannot preserve an invalid detector journal") from exc


def restore(directory: Path, *, bootstrap: bool, seed: Path | None = None) -> None:
    repository, run_id = context()
    prefix = f"repos/{repository}/actions"
    current = api(f"{prefix}/runs/{run_id}")
    if current["head_branch"] != TRUSTED_BRANCH or current["event"] not in EVENTS:
        raise StateError("Observation must execute the trusted production workflow")
    if (current.get("head_repository") or {}).get("full_name") != repository:
        raise StateError("Current workflow repository mismatch")
    # Include failures and cancellations: using only successful runs erases
    # overdue observations, whose expected final conclusion is failure.
    prior = previous_observation(prefix, current, repository)
    if prior is None:
        if not bootstrap or current["event"] != "workflow_dispatch":
            raise StateError("Initial journal requires an explicit bootstrap dispatch")
        if seed is None or not seed.is_file():
            raise StateError("Bootstrap requires the reviewed migrated legacy journal")
        validate_journal(seed)
        journal = seed.read_bytes()
    else:
        artifacts = api(f"{prefix}/runs/{prior['id']}/artifacts?per_page=100")[
            "artifacts"
        ]
        matching = [item for item in artifacts if item["name"] == ARTIFACT]
        if len(matching) != 1 or matching[0]["expired"]:
            raise StateError("Previous observation has no unique unexpired journal")
        item = matching[0]
        if item["size_in_bytes"] > MAX_BYTES:
            raise StateError("Previous journal exceeds size limit")
        archive = api(f"{prefix}/artifacts/{int(item['id'])}/zip", binary=True)
        journal = unpack_state(archive, repository=repository, run_id=prior["id"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / JOURNAL).write_bytes(journal)


def seal(directory: Path) -> None:
    repository, run_id = context()
    journal = (directory / JOURNAL).read_bytes()
    validate_journal(directory / JOURNAL)
    # An equal pair on the first scan legitimately has an empty delta journal.
    metadata = {
        "schema_version": 1,
        "repository": repository,
        "run_id": run_id,
        "sha256": hashlib.sha256(journal).hexdigest(),
    }
    (directory / METADATA).write_text(json.dumps(metadata, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["restore", "seal"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--seed", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "restore":
            restore(args.directory, bootstrap=args.bootstrap, seed=args.seed)
        else:
            seal(args.directory)
    except (StateError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"Release drift state unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
