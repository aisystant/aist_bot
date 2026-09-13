"""The observer must preserve failed/overdue evidence without resetting time."""

import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release_drift_ci", Path(__file__).parents[1] / "scripts/release_drift_ci.py"
)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
REPO = "example/bot"


def run(number, **overrides):
    return {
        "id": number * 10,
        "run_number": number,
        "workflow_id": 7,
        "head_branch": "new-architecture",
        "head_repository": {"full_name": REPO},
        "event": "schedule",
        "status": "completed",
        "conclusion": "success",
        **overrides,
    }


def archive(
    journal=b'{"first_seen_unmatched_at":"2026-09-09T00:00:00Z"}\n', **overrides
):
    stream = io.BytesIO()
    metadata = {
        "schema_version": 1,
        "repository": REPO,
        "run_id": 20,
        "sha256": hashlib.sha256(journal).hexdigest(),
        **overrides,
    }
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr(ci.JOURNAL, journal)
        bundle.writestr(ci.METADATA, json.dumps(metadata))
    return stream.getvalue()


def environment(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", "30")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")


def test_failed_overdue_run_remains_authoritative():
    previous = run(2, conclusion="failure")
    assert ci.preceding_run([run(1), previous, run(3)], run(3), REPO) == previous


def test_wrong_branch_fork_and_workflow_cannot_supply_state():
    candidates = [run(1), run(2, head_branch="pilot"), run(2, workflow_id=8)]
    candidates.append(run(2, head_repository={"full_name": "attacker/bot"}))
    assert ci.preceding_run(candidates, run(3), REPO) == run(1)


def test_skipped_activation_runs_do_not_prevent_first_bootstrap():
    assert ci.preceding_run([run(1, conclusion="skipped")], run(2), REPO) is None


def test_overlapping_observation_blocks():
    with pytest.raises(ci.StateError, match="not completed"):
        ci.preceding_run([run(2, status="in_progress")], run(3), REPO)


def test_roundtrip_preserves_original_sla_bytes():
    expected = b'{"first_seen_unmatched_at":"2026-09-09T00:00:00Z"}\n'
    assert ci.unpack_state(archive(expected), repository=REPO, run_id=20) == expected


@pytest.mark.parametrize(
    "change", [{"run_id": 30}, {"repository": "attacker/bot"}, {"sha256": "0" * 64}]
)
def test_state_provenance_and_checksum_mismatch_rejected(change):
    with pytest.raises(ci.StateError, match="mismatch"):
        ci.unpack_state(archive(**change), repository=REPO, run_id=20)


def test_zip_path_traversal_rejected_without_extraction(tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr("../outside", "unsafe")
    with pytest.raises(ci.StateError):
        ci.unpack_state(data.getvalue(), repository=REPO, run_id=20)
    assert not (tmp_path.parent / "outside").exists()


def test_empty_first_scan_can_be_sealed_and_recovered(tmp_path, monkeypatch):
    environment(monkeypatch)
    (tmp_path / ci.JOURNAL).write_bytes(b"")
    ci.seal(tmp_path)
    metadata = json.loads((tmp_path / ci.METADATA).read_text())
    assert metadata["sha256"] == hashlib.sha256(b"").hexdigest()
    assert ci.unpack_state(archive(b""), repository=REPO, run_id=20) == b""


def test_rerun_cannot_rewind_journal(monkeypatch):
    environment(monkeypatch)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    with pytest.raises(ci.StateError, match="do not rerun"):
        ci.context()


def test_initialization_requires_explicit_manual_dispatch(tmp_path, monkeypatch):
    environment(monkeypatch)
    monkeypatch.setattr(
        ci,
        "api",
        lambda path: run(3) if path.endswith("runs/30") else {"workflow_runs": []},
    )
    with pytest.raises(ci.StateError, match="explicit bootstrap"):
        ci.restore(tmp_path, bootstrap=True)
    assert not (tmp_path / ci.JOURNAL).exists()


def test_bootstrap_cannot_erase_missing_previous_state(tmp_path, monkeypatch):
    environment(monkeypatch)

    def fake_api(path):
        if path.endswith("runs/30"):
            return run(3, event="workflow_dispatch")
        if "/jobs" in path:
            return {"total_count": 1, "jobs": []}
        if "artifacts" in path:
            return {"artifacts": []}
        return {"workflow_runs": [run(2, conclusion="failure")]}

    monkeypatch.setattr(ci, "api", fake_api)
    with pytest.raises(ci.StateError, match="no unique unexpired"):
        ci.restore(tmp_path, bootstrap=True)
    assert not (tmp_path / ci.JOURNAL).exists()


def test_restore_uses_failed_run_not_last_success(tmp_path, monkeypatch):
    environment(monkeypatch)

    def fake_api(path, *, binary=False):
        if binary:
            assert path.endswith("artifacts/99/zip")
            return archive()
        if path.endswith("runs/30"):
            return run(3)
        if "/jobs" in path:
            return {"total_count": 1, "jobs": []}
        if "runs/20/artifacts" in path:
            return {
                "artifacts": [
                    {
                        "id": 99,
                        "name": ci.ARTIFACT,
                        "expired": False,
                        "size_in_bytes": 500,
                    }
                ]
            }
        return {"workflow_runs": [run(1), run(2, conclusion="failure")]}

    monkeypatch.setattr(ci, "api", fake_api)
    ci.restore(tmp_path, bootstrap=False)
    assert b"2026-09-09T00:00:00Z" in (tmp_path / ci.JOURNAL).read_bytes()


@pytest.mark.parametrize("job_count, expected_number", [(0, 1), (1, 2)])
def test_only_never_started_cancelled_run_can_be_skipped(
    monkeypatch, job_count, expected_number
):
    def fake_api(path):
        if "/jobs" in path:
            return {"total_count": job_count}
        return {"workflow_runs": [run(2, conclusion="cancelled"), run(1)]}

    monkeypatch.setattr(ci, "api", fake_api)
    previous = ci.previous_observation("repos/example/bot/actions", run(3), REPO)
    assert previous["run_number"] == expected_number


def test_restore_walks_past_skipped_page(monkeypatch):
    def fake_api(path):
        if "/jobs" in path:
            return {"total_count": 1, "jobs": []}
        if "page=2" in path:
            return {"workflow_runs": [run(1)]}
        return {
            "workflow_runs": [run(n, conclusion="skipped") for n in range(101, 1, -1)]
        }

    monkeypatch.setattr(ci, "api", fake_api)
    assert ci.previous_observation("prefix", run(102), REPO)["run_number"] == 1


def test_job_skipped_inside_successful_run_allows_bootstrap(monkeypatch):
    def fake_api(path):
        if "/jobs" in path:
            return {
                "total_count": 1,
                "jobs": [
                    {
                        "name": "Release drift observation",
                        "conclusion": "skipped",
                        "steps": [],
                    }
                ],
            }
        return {"workflow_runs": [run(1)]}

    monkeypatch.setattr(ci, "api", fake_api)
    assert ci.previous_observation("prefix", run(2), REPO) is None


def test_out_of_order_execution_cannot_rewind_state():
    with pytest.raises(ci.StateError, match="newer run already completed"):
        ci.preceding_run([run(2), run(4)], run(3), REPO)


def test_future_queued_run_does_not_block_current_observer():
    assert ci.preceding_run([run(2), run(4, status="queued")], run(3), REPO) == run(2)


def test_corrupted_journal_cannot_be_sealed(tmp_path, monkeypatch):
    environment(monkeypatch)
    (tmp_path / ci.JOURNAL).write_text('{"partial":')
    with pytest.raises(ci.StateError, match="invalid detector journal"):
        ci.seal(tmp_path)
    assert not (tmp_path / ci.METADATA).exists()


def test_bootstrap_cannot_silently_start_without_legacy_journal(tmp_path, monkeypatch):
    environment(monkeypatch)
    monkeypatch.setattr(
        ci,
        "api",
        lambda path: (
            run(3, event="workflow_dispatch")
            if path.endswith("runs/30")
            else {"workflow_runs": []}
        ),
    )
    with pytest.raises(ci.StateError, match="reviewed migrated legacy journal"):
        ci.restore(tmp_path, bootstrap=True)
    assert not (tmp_path / ci.JOURNAL).exists()


def test_bootstrap_preserves_reviewed_journal_bytes(tmp_path, monkeypatch):
    environment(monkeypatch)
    monkeypatch.setattr(
        ci,
        "api",
        lambda path: (
            run(3, event="workflow_dispatch")
            if path.endswith("runs/30")
            else {"workflow_runs": []}
        ),
    )
    seed = tmp_path / "seed.jsonl"
    # Empty is a valid reviewed baseline only when no delta has been observed.
    seed.write_bytes(b"")
    ci.restore(tmp_path / "state", bootstrap=True, seed=seed)
    assert (tmp_path / "state" / ci.JOURNAL).read_bytes() == seed.read_bytes()
