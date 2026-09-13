#!/usr/bin/env python3
"""Record pilot/production drift without mutating either branch (WP-562).

Patch IDs explain history differences; exact trees establish convergence. A
missing commit alone never proves reconciliation. Exit codes: 0 checked, 1
operational failure, 2 requested alert not delivered, 3 unresolved SLA overdue.
"""

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST_PATH = (
    pathlib.Path.home() / "IWE/DS-my-strategy/machine/release-drift-manifest.jsonl"
)
SLA_HOURS = 24
DIRECTIONS = ("pilot_only", "new_architecture_only", "tree_difference")
POLICY_KEYWORDS = re.compile(
    r"auth|privacy|token|secret|encrypt|gdpr|migrat|permission|password|oauth|payment|consent",
    re.IGNORECASE,
)
OVERRIDE_MARKERS = re.compile(r"\[(incident-ok|cherry-pick-ok)\]")


class DetectorError(RuntimeError):
    """The detector cannot produce trustworthy evidence."""


class Git:
    def __init__(self, repo: pathlib.Path, timeout: float):
        self.repo = repo
        self.timeout = timeout

    def run(self, *args: str, input_text: str | None = None) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                input=input_text,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DetectorError(
                f"git {args[0]} timed out after {self.timeout:g}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            # Remote diagnostics can contain credential-bearing URLs.
            raise DetectorError(
                f"git {args[0]} failed (exit {exc.returncode})"
            ) from exc
        return result.stdout

    def commit(self, ref: str) -> str:
        return self.run(
            "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
        ).strip()

    def cherry(self, upstream: str, head: str) -> list[tuple[str, str, str]]:
        rows = []
        for line in self.run("cherry", "-v", upstream, head).splitlines():
            sign, sha, *subject = line.split(" ", 2)
            if sign not in {"+", "-"}:
                raise DetectorError("Unexpected git cherry output")
            rows.append((sign, sha, subject[0] if subject else ""))
        return rows

    def patch_id(self, sha: str) -> str:
        patch = self.run("show", "--pretty=format:", "--no-ext-diff", "--binary", sha)
        result = self.run("patch-id", "--stable", input_text=patch).split()
        return result[0] if result else f"empty:{sha}"


def timestamp(value: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def has_resolution_evidence(record: dict) -> bool:
    evidence = record.get("resolution_evidence", {})
    return (
        isinstance(evidence, dict)
        and evidence.get("kind") == "tree_equal"
        and isinstance(evidence.get("pilot_tree"), str)
        and bool(evidence.get("pilot_tree"))
        and evidence.get("pilot_tree") == evidence.get("production_tree")
    )


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant: {value}")


def parse_manifest_record(line: str, line_number: int) -> dict:
    """Validate one complete record without interpreting historical changes."""
    try:
        if not line.endswith("\n"):
            raise ValueError("record is missing its terminating newline")
        record = json.loads(
            line, object_pairs_hook=unique_object, parse_constant=reject_json_constant
        )
        if not isinstance(record, dict):
            raise TypeError("record must be an object")
        if not isinstance(record.get("delta_id"), str) or not record["delta_id"]:
            raise ValueError("delta_id is required")
        if type(record.get("schema_version")) is not int or record[
            "schema_version"
        ] not in {1, 2}:
            raise ValueError("unsupported schema_version")
        if record.get("direction") not in DIRECTIONS:
            raise ValueError("unknown direction")
        if record.get("status") not in {"unmatched", "resolved"}:
            raise ValueError("unknown status")
        if not isinstance(record.get("source_sha"), str):
            raise TypeError("source_sha is required")
        first_seen = timestamp(record["first_seen_unmatched_at"])
        deadline = timestamp(record["sla_deadline_at"])
        timestamp(record["last_rechecked_at"])
        if deadline != first_seen + datetime.timedelta(hours=SLA_HOURS):
            raise ValueError("SLA deadline does not match first_seen")
        return record
    except (KeyError, TypeError, ValueError) as exc:
        raise DetectorError(
            f"Invalid manifest record at line {line_number}: {exc}"
        ) from exc


def load_manifest(path: pathlib.Path) -> dict[str, dict]:
    """Read strictly: malformed evidence must never silently restart an SLA."""
    known: dict[str, dict] = {}
    if not path.exists():
        return known
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            record = parse_manifest_record(line, line_number)
            prior = known.get(record["delta_id"])
            if (
                prior
                and timestamp(record["first_seen_unmatched_at"])
                > timestamp(prior["first_seen_unmatched_at"])
                and (
                    prior["status"] != "resolved" or not has_resolution_evidence(prior)
                )
            ):
                raise DetectorError(
                    f"Invalid manifest record at line {line_number}: "
                    "SLA clock advanced without resolution evidence"
                )
            # Version 1 inferred resolution from a disappearing SHA. That
            # claim is not sufficient evidence to discard its unresolved SLA.
            if record["status"] == "resolved" and not has_resolution_evidence(record):
                record = {
                    **record,
                    "status": "unmatched",
                    "observation": "legacy_resolution_unproven",
                }
            known[record["delta_id"]] = record
    return known


def migrate_legacy_manifest(source: pathlib.Path, destination: pathlib.Path) -> dict:
    """Explicitly recover conservative clocks into a new journal, never in place."""
    if source.resolve() == destination.resolve():
        raise DetectorError("Legacy migration requires a distinct destination")
    if destination.exists() or destination.is_symlink():
        raise DetectorError("Legacy migration destination already exists")
    raw = source.read_bytes()
    known = {}
    earliest = {}
    source_count = 0
    for line_number, line in enumerate(
        raw.decode("utf-8").splitlines(keepends=True), start=1
    ):
        if not line.strip():
            continue
        record = parse_manifest_record(line, line_number)
        if record["schema_version"] != 1:
            raise DetectorError("Legacy migration only accepts schema_version 1")
        delta_id = record["delta_id"]
        prior = known.get(delta_id)
        if prior and prior["direction"] != record["direction"]:
            raise DetectorError("Legacy delta changed direction")
        first_seen = record["first_seen_unmatched_at"]
        if delta_id not in earliest or timestamp(first_seen) < timestamp(
            earliest[delta_id]
        ):
            earliest[delta_id] = first_seen
        known[delta_id] = record
        source_count += 1
    provenance = {
        "kind": "legacy_manifest_migration",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_record_count": source_count,
        "reason": "legacy_resolution_unproven",
        "migrated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    migrated = []
    for delta_id, record in known.items():
        first_seen = earliest[delta_id]
        row = {
            **record,
            "schema_version": 2,
            "status": "unmatched",
            "classification": "unknown",
            "equivalence_level": "none",
            "observation": "legacy_resolution_unproven",
            "provenance": provenance,
            "first_seen_unmatched_at": first_seen,
            "sla_deadline_at": (
                timestamp(first_seen) + datetime.timedelta(hours=SLA_HOURS)
            ).isoformat(),
        }
        row.pop("resolution_evidence", None)
        migrated.append(row)
    # O_EXCL also rejects a destination created after the preflight check.
    with manifest_lock(destination), destination.open("x", encoding="utf-8") as target:
        for row in migrated:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
        target.flush()
        os.fsync(target.fileno())
    return {
        "schema_version": 2,
        "status": "migrated",
        "provenance": provenance,
        "migrated_delta_count": len(migrated),
    }


@contextlib.contextmanager
def manifest_lock(path: pathlib.Path):
    """Serialize the full read/compare/durable-append transaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def append_manifest(path: pathlib.Path, records: list[dict]) -> None:
    if not records:
        return
    with path.open("a", encoding="utf-8") as target:
        for record in records:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
        target.flush()
        os.fsync(target.fileno())


def oldest_first_seen(priors: list[dict], now: str) -> str:
    times = [
        row["first_seen_unmatched_at"]
        for row in priors
        if row["status"] == "unmatched" or not has_resolution_evidence(row)
    ]
    return min(times, key=timestamp) if times else now


def delta_record(
    delta_id: str, direction: str, sha: str, subject: str, first_seen: str, now: str
) -> dict:
    return {
        "schema_version": 2,
        "delta_id": delta_id,
        "direction": direction,
        "source_sha": sha,
        "source_subject": subject,
        "candidate_matches": [],
        "equivalence_level": "none",
        "policy_tags": [],
        "override_flags": [],
        "first_seen_unmatched_at": first_seen,
        "last_rechecked_at": now,
        "status": "unmatched",
        "sla_deadline_at": (
            timestamp(first_seen) + datetime.timedelta(hours=SLA_HOURS)
        ).isoformat(),
        "observation": "present",
    }


def patch_records(
    git: Git,
    direction: str,
    rows: list[tuple[str, str, str]],
    now: str,
    known: dict[str, dict],
) -> list[dict]:
    records = []
    for sign, sha, subject in rows:
        if sign != "+":
            continue
        patch_id = git.patch_id(sha)
        delta_id = f"{direction}:patch:{patch_id}"
        priors = [
            record
            for record in known.values()
            if record["direction"] == direction
            and (record.get("patch_id") == patch_id or record["source_sha"] == sha)
        ]
        # Legacy SHA-only evidence may be impossible to map after a rewrite.
        # Its earlier clock survives conservatively until tree convergence.
        if not priors:
            priors = [
                record
                for record in known.values()
                if record["direction"] == direction
                and record["status"] == "unmatched"
                and not record.get("patch_id")
            ]
        record = delta_record(
            delta_id, direction, sha, subject, oldest_first_seen(priors, now), now
        )
        paths = git.run(
            "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha
        )
        record["patch_id"] = patch_id
        record["policy_tags"] = sorted(
            {
                match.group(0).lower()
                for match in POLICY_KEYWORDS.finditer(subject + "\n" + paths)
            }
        )
        record["override_flags"] = sorted(
            set(OVERRIDE_MARKERS.findall(git.run("log", "-1", "--format=%B", sha)))
        )
        records.append(record)
    return records


def inspect(
    git: Git, manifest: pathlib.Path, pilot_ref: str, production_ref: str
) -> dict:
    with manifest_lock(manifest):
        known = load_manifest(manifest)
        # Resolve once under the journal lock, so a delayed older observation
        # cannot overwrite newer evidence from a concurrent invocation.
        pilot = git.commit(pilot_ref)
        production = git.commit(production_ref)
        pilot_tree = git.run("rev-parse", f"{pilot}^{{tree}}").strip()
        production_tree = git.run("rev-parse", f"{production}^{{tree}}").strip()
        equal_trees = pilot_tree == production_tree
        snapshots = {
            "pilot": {"commit": pilot, "tree": pilot_tree},
            "production": {"commit": production, "tree": production_tree},
        }
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows_by_direction = {
            "pilot_only": git.cherry(production, pilot),
            "new_architecture_only": git.cherry(pilot, production),
        }
        records = []
        if equal_trees:
            evidence = {
                "kind": "tree_equal",
                "pilot_tree": pilot_tree,
                "production_tree": production_tree,
                "snapshots": snapshots,
            }
            records.extend(
                {
                    **prior,
                    "status": "resolved",
                    "last_rechecked_at": now,
                    "equivalence_level": "tree",
                    "resolution_evidence": evidence,
                }
                for prior in known.values()
                if prior["status"] == "unmatched"
            )
        else:
            for direction, rows in rows_by_direction.items():
                records.extend(patch_records(git, direction, rows, now, known))
            tree_id = "tree_difference:pilot-production"
            prior_tree = known.get(tree_id)
            priors = [prior_tree] if prior_tree else list(known.values())
            tree_record = delta_record(
                tree_id,
                "tree_difference",
                pilot,
                "Pilot and production source trees differ",
                oldest_first_seen(priors, now),
                now,
            )
            tree_record["snapshots"] = snapshots
            records.append(tree_record)
            active_ids = {record["delta_id"] for record in records}
            records.extend(
                {
                    **prior,
                    "last_rechecked_at": now,
                    "observation": "not_observed_resolution_unproven",
                }
                for key, prior in known.items()
                if key not in active_ids and prior["status"] == "unmatched"
            )
        append_manifest(manifest, records)

    overdue = [
        row
        for row in records
        if row["status"] == "unmatched"
        and timestamp(row["sla_deadline_at"]) <= timestamp(now)
    ]
    deadlines = [
        row["sla_deadline_at"] for row in records if row["status"] == "unmatched"
    ]
    summary = {}
    for direction in DIRECTIONS:
        scoped = [row for row in records if row["direction"] == direction]
        summary[direction] = {
            "real_delta_count": sum(row["status"] == "unmatched" for row in scoped),
            "equivalent_count": sum(
                sign == "-" for sign, _, _ in rows_by_direction.get(direction, [])
            ),
            "resolved_count": sum(row["status"] == "resolved" for row in scoped),
        }
    return {
        "schema_version": 2,
        "status": "clean" if equal_trees else "drift",
        "checked_at": now,
        "snapshots": snapshots,
        "summary": summary,
        "oldest_deadline_at": min(deadlines, key=timestamp) if deadlines else None,
        "records": records,
        "overdue": overdue,
    }


def send_alert(
    message: str, timeout: float, source: str = "release-drift-detector"
) -> None:
    if (
        os.environ.get("TELEGRAM_ENV") == "test"
        and os.environ.get("ALLOW_LIVE_TELEGRAM_IN_TEST") != "1"
    ):
        raise DetectorError("Requested alert is blocked by TELEGRAM_ENV=test")
    executable = shutil.which("iwe-tg")
    if not executable:
        raise DetectorError("Requested alert unavailable: iwe-tg is not installed")
    try:
        result = subprocess.run(
            [executable, "--source", source, "--", message],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DetectorError("Requested alert delivery timed out") from exc
    if result.returncode:
        raise DetectorError(f"Requested alert failed (exit {result.returncode})")


def write_report(path: pathlib.Path, report: dict) -> None:
    """Replace a report atomically; partial JSON is never an output artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as target:
            pending = pathlib.Path(target.name)
            json.dump(report, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        pending.replace(path)
    finally:
        if pending and pending.exists():
            pending.unlink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, default=REPO_ROOT)
    parser.add_argument("--manifest", type=pathlib.Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--migrate-legacy-manifest",
        type=pathlib.Path,
        metavar="SOURCE",
        help="Migrate version 1 history into a new --manifest; no Git or alert",
    )
    parser.add_argument("--pilot-ref", default="origin/pilot")
    parser.add_argument("--production-ref", default="origin/new-architecture")
    parser.add_argument(
        "--no-fetch", action="store_true", help="Use only pinned local snapshots"
    )
    parser.add_argument(
        "--git-timeout", type=float, default=120, help="Per-command timeout in seconds"
    )
    parser.add_argument(
        "--alert",
        action="store_true",
        help="Deliver a Telegram alert on overdue drift/errors",
    )
    parser.add_argument(
        "--alert-source",
        default="release-drift-detector",
        help="Registered source ID in the iwe-tg transport allowlist",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the full JSON report"
    )
    parser.add_argument(
        "--output", type=pathlib.Path, help="Atomically write the JSON report"
    )
    args = parser.parse_args(argv)
    if not 0 < args.git_timeout <= 3600:
        parser.error("--git-timeout must be between 0 and 3600 seconds")
    if args.output and args.output.resolve() in {
        args.manifest.resolve(),
        args.manifest.with_suffix(args.manifest.suffix + ".lock").resolve(),
    }:
        parser.error("--output must be separate from the manifest and its lock")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.migrate_legacy_manifest:
        try:
            if (
                args.output
                and args.output.resolve() == args.migrate_legacy_manifest.resolve()
            ):
                raise DetectorError("Migration report must not overwrite the source")
            report = migrate_legacy_manifest(
                args.migrate_legacy_manifest, args.manifest
            )
            if args.output:
                write_report(args.output, report)
        except (DetectorError, OSError, UnicodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    try:
        git = Git(args.repo, args.git_timeout)
        if not args.no_fetch:
            git.run("fetch", "--no-tags", "origin")
        report = inspect(git, args.manifest, args.pilot_ref, args.production_ref)
    except (DetectorError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.alert:
            try:
                send_alert(
                    "⚠️ WP-562: детектор расхождений не смог проверить данные. Проверьте журнал запуска.",
                    args.git_timeout,
                    args.alert_source,
                )
            except (DetectorError, OSError) as delivery_error:
                print(f"ERROR: {delivery_error}", file=sys.stderr)
                return 2
        return 1

    exit_code = 3 if report["overdue"] else 0
    report["alert_delivery"] = "not_needed" if args.alert else "not_requested"
    if args.alert and report["overdue"]:
        try:
            send_alert(
                f"⚠️ WP-562: {len(report['overdue'])} неразрешённых записей старше {SLA_HOURS}ч. "
                "Подробности в release-drift-manifest.jsonl.",
                args.git_timeout,
                args.alert_source,
            )
            report["alert_delivery"] = "delivered"
        except (DetectorError, OSError) as exc:
            report["alert_delivery"] = "failed"
            print(f"ERROR: {exc}", file=sys.stderr)
            exit_code = 2
    try:
        if args.output:
            write_report(args.output, report)
    except OSError as exc:
        print(f"ERROR: report was not written: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for direction, stats in report["summary"].items():
            print(
                f"{direction}: {stats['real_delta_count']} unresolved, "
                f"{stats['equivalent_count']} patch-equivalent, {stats['resolved_count']} resolved"
            )
        print(
            f"status={report['status']}; overdue={len(report['overdue'])}; "
            f"oldest_deadline_at={report['oldest_deadline_at']}"
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
