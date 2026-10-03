"""Offline acceptance tests for the release drift evidence and CLI contract."""

import datetime
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release-drift-detector.py"
SPEC = importlib.util.spec_from_file_location("release_drift_detector", SCRIPT)
detector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(detector)


class DetectorAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wp562-detector-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.manifest = self.root / "drift.jsonl"
        self.environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        self.git("init", "-b", "production")
        self.git("config", "user.name", "Offline Test")
        self.git("config", "user.email", "offline@example.invalid")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        self.commit("base.txt", "base\n", "baseline")
        self.baseline = self.git("rev-parse", "HEAD").strip()
        self.git("branch", "pilot")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=10,
        ).stdout

    def commit(self, filename, contents, subject):
        (self.repo / filename).write_text(contents, encoding="utf-8")
        self.git("add", "--", filename)
        self.git("commit", "-m", subject)
        return self.git("rev-parse", "HEAD").strip()

    def diverge(self):
        self.git("checkout", "pilot")
        return self.commit("feature.txt", "feature\n", "feature")

    def command(self, *extra, fetch=False):
        command = [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(self.repo),
            "--manifest",
            str(self.manifest),
            "--pilot-ref",
            "pilot",
            "--production-ref",
            "production",
            "--json",
        ]
        if not fetch:
            command.append("--no-fetch")
        return [*command, *extra]

    def run_detector(self, *extra, fetch=False, environment=None):
        return subprocess.run(
            self.command(*extra, fetch=fetch),
            text=True,
            capture_output=True,
            timeout=15,
            env=environment or self.environment,
            check=False,
        )

    def report(self, *extra, expected=0, **kwargs):
        result = self.run_detector(*extra, **kwargs)
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout)

    def age_manifest(self):
        old = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=26)
        ).isoformat()
        records = [json.loads(line) for line in self.manifest.read_text().splitlines()]
        for row in records:
            row["first_seen_unmatched_at"] = old
            row["sla_deadline_at"] = (
                detector.timestamp(old) + datetime.timedelta(hours=24)
            ).isoformat()
        self.manifest.write_text("".join(json.dumps(row) + "\n" for row in records))
        return old

    def fake_transport(self, exit_code):
        executable = self.root / "iwe-tg"
        marker = self.root / "sent.txt"
        executable.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{marker}'\nexit {exit_code}\n"
        )
        executable.chmod(0o755)
        environment = {**self.environment, "PATH": f"{self.root}:{os.environ['PATH']}"}
        environment.pop("TELEGRAM_ENV", None)
        return environment, marker

    def test_explicit_snapshots_need_no_remote_and_output_matches(self):
        self.diverge()
        before_refs = self.git("show-ref")
        before_status = self.git("status", "--porcelain")
        output = self.root / "report.json"
        report = self.report("--output", str(output))
        self.assertEqual(report, json.loads(output.read_text()))
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["summary"]["tree_difference"]["real_delta_count"], 1)
        self.assertEqual(
            report["snapshots"]["pilot"]["commit"],
            self.git("rev-parse", "pilot").strip(),
        )
        self.assertEqual(self.git("show-ref"), before_refs)
        self.assertEqual(self.git("status", "--porcelain"), before_status)
        self.assertEqual(report["alert_delivery"], "not_requested")

    def test_equal_trees_with_different_shas_are_clean_and_close_with_evidence(self):
        feature = self.diverge()
        self.report()
        self.git("checkout", "production")
        self.git("cherry-pick", feature)
        self.git("commit", "--amend", "-m", "independent production history")
        report = self.report()
        self.assertNotEqual(
            report["snapshots"]["pilot"]["commit"],
            report["snapshots"]["production"]["commit"],
        )
        self.assertEqual(report["status"], "clean")
        self.assertIsNone(report["oldest_deadline_at"])
        self.assertGreater(len(report["records"]), 0)
        for row in report["records"]:
            self.assertEqual(row["status"], "resolved")
            self.assertTrue(detector.has_resolution_evidence(row))

    def test_patch_id_preserves_sla_after_sha_rewrite(self):
        original_sha = self.diverge()
        first = self.report()
        old = self.age_manifest()
        self.git("commit", "--amend", "-m", "same patch under a new SHA")
        report = self.report(expected=3)
        before = next(
            row for row in first["records"] if row["direction"] == "pilot_only"
        )
        after = next(
            row for row in report["records"] if row["direction"] == "pilot_only"
        )
        self.assertEqual(before["delta_id"], after["delta_id"])
        self.assertNotEqual(original_sha, after["source_sha"])
        self.assertEqual(after["first_seen_unmatched_at"], old)
        self.assertEqual(report["oldest_deadline_at"], after["sla_deadline_at"])

    def test_legacy_sha_clock_survives_rewrite_without_mapping(self):
        original = self.diverge()
        first = self.report()
        legacy = next(
            row for row in first["records"] if row["direction"] == "pilot_only"
        )
        legacy["schema_version"] = 1
        legacy["delta_id"] = f"pilot_only:{original}"
        del legacy["patch_id"]
        self.manifest.write_text(json.dumps(legacy) + "\n")
        old = self.age_manifest()
        self.git("commit", "--amend", "-m", "rewritten legacy")
        report = self.report(expected=3)
        records = [row for row in report["records"] if row["direction"] == "pilot_only"]
        self.assertTrue(all(row["first_seen_unmatched_at"] == old for row in records))
        self.assertTrue(all(row["status"] == "unmatched" for row in records))

    def test_missing_commit_does_not_resolve_when_trees_still_differ(self):
        self.diverge()
        first = self.report()
        original = next(
            row for row in first["records"] if row["direction"] == "pilot_only"
        )
        self.age_manifest()
        self.git("reset", "--hard", self.baseline)
        self.commit("other.txt", "unrelated\n", "new unrelated change")
        report = self.report(expected=3)
        retained = next(
            row for row in report["records"] if row["delta_id"] == original["delta_id"]
        )
        self.assertEqual(retained["status"], "unmatched")
        self.assertEqual(retained["observation"], "not_observed_resolution_unproven")

    def test_merge_only_change_cannot_hide_behind_git_cherry(self):
        self.git("checkout", "-b", "side", self.baseline)
        side = self.commit("side.txt", "side\n", "side change")
        self.git("checkout", "pilot")
        main = self.commit("main.txt", "main\n", "main change")
        self.git("merge", "--no-ff", "--no-commit", "side")
        self.commit("merge-only.txt", "merge resolution\n", "merge with extra change")
        self.git("checkout", "production")
        self.git("cherry-pick", main, side)
        self.assertFalse(
            any(
                line.startswith("+")
                for line in self.git("cherry", "production", "pilot").splitlines()
            )
        )
        report = self.report()
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["summary"]["tree_difference"]["real_delta_count"], 1)
        self.assertTrue(
            any(
                row["direction"] == "tree_difference" and row["status"] == "unmatched"
                for row in report["records"]
            )
        )

    def test_reverted_patch_still_has_tree_drift(self):
        feature = self.diverge()
        self.git("checkout", "production")
        self.git("cherry-pick", feature)
        self.git("revert", "--no-edit", "HEAD")
        self.assertFalse(
            any(
                line.startswith("+")
                for line in self.git("cherry", "production", "pilot").splitlines()
            )
        )
        report = self.report()
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["summary"]["tree_difference"]["real_delta_count"], 1)

    def test_corrupt_manifest_fails_without_append(self):
        self.diverge()
        self.report()
        with self.manifest.open("a") as target:
            target.write('{"delta_id":')
        before = self.manifest.read_bytes()
        result = self.run_detector()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Invalid manifest record", result.stderr)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_truncated_line_after_complete_json_is_not_appended_to(self):
        self.diverge()
        self.report()
        self.manifest.write_bytes(self.manifest.read_bytes().rstrip(b"\n"))
        before = self.manifest.read_bytes()
        result = self.run_detector()
        self.assertEqual(result.returncode, 1)
        self.assertIn("terminating newline", result.stderr)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_legacy_resolution_without_evidence_cannot_restart_clock(self):
        self.diverge()
        self.report()
        old = self.age_manifest()
        records = [json.loads(line) for line in self.manifest.read_text().splitlines()]
        for row in records:
            row["status"] = "resolved"
        self.manifest.write_text("".join(json.dumps(row) + "\n" for row in records))
        self.git("commit", "--amend", "-m", "new SHA after false legacy resolution")
        report = self.report(expected=3)
        self.assertTrue(
            all(row["first_seen_unmatched_at"] == old for row in report["records"])
        )
        self.assertTrue(all(row["status"] == "unmatched" for row in report["records"]))

    def test_proven_tree_convergence_allows_new_drift_window(self):
        feature = self.diverge()
        self.report()
        old = self.age_manifest()
        self.git("checkout", "production")
        self.git("cherry-pick", feature)
        self.report()
        self.git("revert", "--no-edit", "HEAD")
        report = self.report()
        self.assertEqual(report["overdue"], [])
        self.assertTrue(
            all(
                detector.timestamp(row["first_seen_unmatched_at"])
                > detector.timestamp(old)
                for row in report["records"]
            )
        )

    def test_semantically_invalid_json_is_not_treated_as_empty_history(self):
        self.diverge()
        row = self.report()["records"][0]
        for field, value in (
            ("status", "approved"),
            ("first_seen_unmatched_at", None),
            ("sla_deadline_at", "2026-01-01T00:00:00"),
            ("direction", "unknown"),
            ("schema_version", 99),
        ):
            with self.subTest(field=field):
                self.manifest.write_text(json.dumps({**row, field: value}) + "\n")
                before = self.manifest.read_bytes()
                result = self.run_detector()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.manifest.read_bytes(), before)

    def test_sla_clock_cannot_advance_without_resolution_evidence(self):
        self.diverge()
        row = self.report()["records"][0]
        newer = {**row}
        for key in ("first_seen_unmatched_at", "sla_deadline_at", "last_rechecked_at"):
            newer[key] = (
                detector.timestamp(row[key]) + datetime.timedelta(hours=1)
            ).isoformat()
        self.manifest.write_text(json.dumps(row) + "\n" + json.dumps(newer) + "\n")
        result = self.run_detector()
        self.assertEqual(result.returncode, 1)
        self.assertIn("SLA clock advanced", result.stderr)

    def test_fetch_error_without_alert_never_calls_transport(self):
        environment, marker = self.fake_transport(0)
        result = self.run_detector(fetch=True, environment=environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("git fetch failed", result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(self.manifest.exists())

    def test_overdue_delivery_failure_is_visible_in_output_and_exit(self):
        self.diverge()
        self.report()
        self.age_manifest()
        environment, marker = self.fake_transport(3)
        report = self.report("--alert", expected=2, environment=environment)
        self.assertEqual(report["alert_delivery"], "failed")
        self.assertGreater(len(report["overdue"]), 0)
        self.assertTrue(marker.exists())

    def test_successful_delivery_keeps_overdue_exit_code(self):
        self.diverge()
        self.report()
        self.age_manifest()
        environment, marker = self.fake_transport(0)
        report = self.report("--alert", expected=3, environment=environment)
        self.assertEqual(report["alert_delivery"], "delivered")
        self.assertTrue(marker.exists())
        self.assertIn("--source\nrelease-drift-detector\n--\n", marker.read_text())

    def test_blocked_test_transport_is_not_claimed_as_delivered(self):
        self.diverge()
        self.report()
        self.age_manifest()
        environment, marker = self.fake_transport(0)
        environment.update(TELEGRAM_ENV="test", ALLOW_LIVE_TELEGRAM_IN_TEST="0")
        report = self.report("--alert", expected=2, environment=environment)
        self.assertEqual(report["alert_delivery"], "failed")
        self.assertFalse(marker.exists())

    def test_missing_transport_is_not_success(self):
        with (
            mock.patch.object(detector.shutil, "which", return_value=None),
            self.assertRaisesRegex(detector.DetectorError, "not installed"),
        ):
            detector.send_alert("synthetic", 1)

    def test_git_timeout_is_bounded_and_does_not_leak_command_output(self):
        with mock.patch.object(
            detector.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("git", 0.1),
        ) as run:
            with self.assertRaisesRegex(detector.DetectorError, "timed out"):
                detector.Git(self.repo, 0.1).run("fetch", "origin")
            self.assertEqual(run.call_args.kwargs["timeout"], 0.1)

    def test_append_is_flushed_before_fsync(self):
        observed = []
        real_fsync = os.fsync

        def inspect_fsync(fd):
            observed.append(self.manifest.read_text())
            real_fsync(fd)

        with (
            mock.patch.object(detector.os, "fsync", side_effect=inspect_fsync),
            detector.manifest_lock(self.manifest),
        ):
            detector.append_manifest(self.manifest, [{"delta_id": "synthetic"}])
        self.assertEqual(json.loads(observed[0]), {"delta_id": "synthetic"})

    def test_overlapping_runs_keep_one_first_seen_and_valid_jsonl(self):
        self.diverge()
        processes = [
            subprocess.Popen(
                self.command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environment,
            )
            for _ in range(3)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "drift")
        rows = [json.loads(line) for line in self.manifest.read_text().splitlines()]
        clocks = {}
        for row in rows:
            clocks.setdefault(row["delta_id"], set()).add(
                row["first_seen_unmatched_at"]
            )
        self.assertTrue(all(len(values) == 1 for values in clocks.values()))

    def legacy_history(self):
        first_seen = "2026-09-01T00:00:00+00:00"
        now = "2026-09-05T00:00:00+00:00"
        row = detector.delta_record(
            "pilot_only:legacy",
            "pilot_only",
            self.baseline,
            "synthetic legacy",
            first_seen,
            first_seen,
        )
        row["schema_version"] = 1
        falsely_resolved = {**row, "status": "resolved", "last_rechecked_at": now}
        reset = {
            **row,
            "first_seen_unmatched_at": now,
            "last_rechecked_at": now,
            "sla_deadline_at": "2026-09-06T00:00:00+00:00",
        }
        source = self.root / "legacy.jsonl"
        source.write_text(
            "".join(json.dumps(item) + "\n" for item in (row, falsely_resolved, reset))
        )
        return source, first_seen

    def test_explicit_legacy_migration_preserves_earliest_clock_and_source(self):
        source, first_seen = self.legacy_history()
        original = source.read_bytes()
        with self.assertRaisesRegex(detector.DetectorError, "SLA clock advanced"):
            detector.load_manifest(source)
        report = self.report("--migrate-legacy-manifest", str(source))
        self.assertEqual(report["status"], "migrated")
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(
            report["provenance"]["source_sha256"], hashlib.sha256(original).hexdigest()
        )
        migrated = detector.load_manifest(self.manifest)["pilot_only:legacy"]
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["first_seen_unmatched_at"], first_seen)
        self.assertEqual(migrated["sla_deadline_at"], "2026-09-02T00:00:00+00:00")
        self.assertEqual(migrated["status"], "unmatched")
        self.assertEqual(migrated["classification"], "unknown")

    def test_migration_reopens_legacy_resolved_record(self):
        source, first_seen = self.legacy_history()
        rows = source.read_text().splitlines()
        source.write_text("\n".join(rows[:2]) + "\n")
        detector.migrate_legacy_manifest(source, self.manifest)
        row = detector.load_manifest(self.manifest)["pilot_only:legacy"]
        self.assertEqual(row["status"], "unmatched")
        self.assertEqual(row["first_seen_unmatched_at"], first_seen)
        self.assertEqual(row["provenance"]["reason"], "legacy_resolution_unproven")

    def test_migration_refuses_same_path_existing_destination_and_source_report(self):
        source, _ = self.legacy_history()
        before = source.read_bytes()
        with self.assertRaisesRegex(detector.DetectorError, "distinct destination"):
            detector.migrate_legacy_manifest(source, source)
        self.manifest.write_text("existing\n")
        with self.assertRaisesRegex(detector.DetectorError, "already exists"):
            detector.migrate_legacy_manifest(source, self.manifest)
        result = self.run_detector(
            "--migrate-legacy-manifest", str(source), "--output", str(source)
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.manifest.read_text(), "existing\n")

    def test_migration_rejects_corruption_nonlegacy_and_missing_fields(self):
        source, _ = self.legacy_history()
        valid = source.read_text().splitlines()[0]
        record = json.loads(valid)
        variants = [
            valid.rstrip("\n"),
            '{"broken":\n',
            json.dumps({**record, "schema_version": 2}) + "\n",
            json.dumps({**record, "first_seen_unmatched_at": "2026-09-01T00:00:00"})
            + "\n",
            '{"schema_version":1,"schema_version":1}\n',
            '{"x":NaN}\n',
            json.dumps({"schema_version": 1}) + "\n",
        ]
        for malformed in variants:
            with self.subTest(malformed=malformed):
                source.write_text(malformed)
                with self.assertRaises(detector.DetectorError):
                    detector.migrate_legacy_manifest(source, self.manifest)
                self.assertFalse(self.manifest.exists())

    def test_migration_never_uses_git_or_alert_transport(self):
        source, _ = self.legacy_history()
        environment, marker = self.fake_transport(0)
        report = self.report(
            "--migrate-legacy-manifest",
            str(source),
            "--alert",
            "--repo",
            str(self.root / "missing-repo"),
            fetch=True,
            environment=environment,
        )
        self.assertEqual(report["status"], "migrated")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
