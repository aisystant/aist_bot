#!/usr/bin/env python3
"""Two-way semantic drift detector between pilot and new-architecture (WP-562 Ф2).

Equivalence is decided by git's own patch-id comparison (`git cherry`), not by
counting unique SHAs: a commit cherry-picked or rebased across branches gets a
new SHA but the same patch-id, so a naive SHA diff overstates drift (proven live
03.09.2026: 21+22 SHA-unique commits, only 1 real content difference that day).

Unmatched commits ("+" in `git cherry -v`) are the real delta. Each one is
tagged for security/privacy-sensitive paths (a heuristic keyword pass, not a
full policy engine — see report.md for the scope this leaves open) and checked
for an `[incident-ok]`/`[cherry-pick-ok]` override marker in its message, so a
bypass of the local pre-push drift guard leaves an auditable trail here even
when the guard itself doesn't refuse it.

Findings are appended to a JSONL manifest (append-only; last record per
delta_id wins on read) so `first_seen_unmatched_at` survives reruns and an
alert can fire once a delta has sat unresolved past the SLA.
"""

import argparse
import contextlib
import datetime
import fcntl
import json
import pathlib
import shutil
import subprocess
import sys
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST_PATH = pathlib.Path.home() / "IWE/DS-my-strategy/machine/release-drift-manifest.jsonl"
SLA_HOURS = 24

DIRECTIONS = [
    # (label, upstream, head) — head-only commits (by patch-id) are the delta
    ("pilot_only", "origin/new-architecture", "origin/pilot"),
    ("new_architecture_only", "origin/pilot", "origin/new-architecture"),
]

POLICY_KEYWORDS = re.compile(
    r"auth|privacy|token|secret|encrypt|gdpr|migrat|permission|password|oauth|payment|consent",
    re.IGNORECASE,
)
OVERRIDE_MARKERS = re.compile(r"\[(incident-ok|cherry-pick-ok)\]")


def git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return result.stdout


def cherry(upstream: str, head: str) -> list[tuple[str, str, str]]:
    """Return (sign, sha, subject) triples; '+' = real delta, '-' = patch-id match."""
    rows = []
    for line in git("cherry", "-v", upstream, head).splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 2)
        sign, sha = parts[0], parts[1]
        subject = parts[2] if len(parts) > 2 else ""
        rows.append((sign, sha, subject))
    return rows


def changed_paths(sha: str) -> list[str]:
    # --name-only (not --stat): --stat truncates long paths for display, which
    # would silently hide a policy keyword living in a truncated segment.
    output = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    return [line for line in output.splitlines() if line.strip()]


def policy_tags(sha: str, subject: str) -> list[str]:
    texts = [subject, *changed_paths(sha)]
    return sorted({m.group(0).lower() for text in texts for m in POLICY_KEYWORDS.finditer(text)})


def override_flags(sha: str) -> list[str]:
    message = git("log", "-1", "--format=%B", sha)
    return sorted(set(OVERRIDE_MARKERS.findall(message)))


def load_manifest() -> dict[str, dict]:
    """Latest record per delta_id — append-only file, later lines win."""
    known: dict[str, dict] = {}
    if MANIFEST_PATH.exists():
        for line_number, line in enumerate(MANIFEST_PATH.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                # A killed writer or a mid-write crash can leave a truncated
                # trailing line. One bad line must not permanently break every
                # future run — skip it and keep going.
                print(f"WARN: {MANIFEST_PATH}:{line_number} нечитаемая строка манифеста пропущена: {exc}", file=sys.stderr)
                continue
            known[record["delta_id"]] = record
    return known


def append_manifest(records: list[dict]) -> None:
    if not records:
        return
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_PATH.open("a") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def manifest_lock():
    """Serialize the read-modify-append cycle across overlapping invocations
    (cron overlapping a manual run, two runners racing) — without it, two
    processes can both see a delta as unseen and each write a different
    first_seen_unmatched_at, corrupting the SLA clock."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = MANIFEST_PATH.with_suffix(MANIFEST_PATH.suffix + ".lock")
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def build_delta_records(direction: str, deltas: list[tuple[str, str]], now: str, known: dict[str, dict]) -> list[dict]:
    records = []
    for sha, subject in deltas:
        delta_id = f"{direction}:{sha}"
        prior = known.get(delta_id)
        # Only inherit first_seen from a prior UNMATCHED record. A delta that
        # was previously resolved and has now reopened (possible across
        # history rewrites) must restart its SLA clock — otherwise it can be
        # reported as already overdue on the very run it reopens.
        first_seen = prior["first_seen_unmatched_at"] if prior and prior["status"] == "unmatched" else now
        sla_deadline = (
            datetime.datetime.fromisoformat(first_seen) + datetime.timedelta(hours=SLA_HOURS)
        ).isoformat()
        records.append(
            {
                "schema_version": 1,
                "delta_id": delta_id,
                "direction": direction,
                "source_sha": sha,
                "source_subject": subject,
                "candidate_matches": [],
                "equivalence_level": "none",
                "policy_tags": policy_tags(sha, subject),
                "override_flags": override_flags(sha),
                "first_seen_unmatched_at": first_seen,
                "last_rechecked_at": now,
                "status": "unmatched",
                "sla_deadline_at": sla_deadline,
            }
        )
    return records


def build_resolved_records(direction: str, active_delta_ids: set[str], now: str, known: dict[str, dict]) -> list[dict]:
    """A delta_id that used to be unmatched and no longer shows up got promoted or reconciled."""
    resolved = []
    for delta_id, prior in known.items():
        if prior["direction"] == direction and prior["status"] == "unmatched" and delta_id not in active_delta_ids:
            resolved.append({**prior, "last_rechecked_at": now, "status": "resolved"})
    return resolved


def send_alert(message: str) -> None:
    if not shutil.which("iwe-tg"):
        print(f"INFO: iwe-tg недоступен, alert только в лог: {message}", file=sys.stderr)
        return
    result = subprocess.run(["iwe-tg", message])
    if result.returncode != 0:
        print(f"WARN: iwe-tg вернул код {result.returncode}, alert не доставлен: {message}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert", action="store_true", help="Send a Telegram alert for deltas overdue past the SLA")
    parser.add_argument("--json", action="store_true", help="Print a JSON summary instead of human-readable text")
    args = parser.parse_args()

    try:
        git("fetch", "origin", "pilot", "new-architecture")
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        # A broken detector is worse than a silent one — without this alert,
        # a persistent fetch failure (network, host down) freezes the whole
        # drift pipeline with nobody told.
        send_alert(f"⚠️ WP-562 Ф2: детектор расхождений pilot↔production не смог обновить данные ({exc}). Проверка приостановлена.")
        sys.exit(1)

    all_new_records: list[dict] = []
    summary: dict[str, dict] = {}

    # Locked for the whole read-modify-append span: two overlapping runs (cron
    # racing a manual invocation) must not both see a delta as unseen and each
    # write a different first_seen_unmatched_at.
    with manifest_lock():
        known = load_manifest()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        for direction, upstream, head in DIRECTIONS:
            rows = cherry(upstream, head)
            deltas = [(sha, subject) for sign, sha, subject in rows if sign == "+"]
            equivalent_count = sum(1 for sign, _, _ in rows if sign == "-")

            delta_records = build_delta_records(direction, deltas, now, known)
            active_ids = {r["delta_id"] for r in delta_records}
            resolved_records = build_resolved_records(direction, active_ids, now, known)

            all_new_records.extend(delta_records)
            all_new_records.extend(resolved_records)
            summary[direction] = {
                "real_delta_count": len(deltas),
                "equivalent_count": equivalent_count,
                "resolved_count": len(resolved_records),
            }

        append_manifest(all_new_records)

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    overdue = [
        r
        for r in all_new_records
        if r["status"] == "unmatched" and datetime.datetime.fromisoformat(r["sla_deadline_at"]) < now_dt
    ]

    if args.json:
        print(json.dumps({"summary": summary, "records": all_new_records, "overdue": overdue}, ensure_ascii=False, indent=2))
    else:
        for direction, stats in summary.items():
            print(
                f"{direction}: {stats['real_delta_count']} реальных дельт, "
                f"{stats['equivalent_count']} эквивалентных по содержанию (разные SHA), "
                f"{stats['resolved_count']} закрыто с прошлого прогона"
            )
        if overdue:
            print(f"\n⚠️  {len(overdue)} дельт(а) не разобраны дольше {SLA_HOURS}ч:")
            for record in overdue:
                tags = f" [{', '.join(record['policy_tags'])}]" if record["policy_tags"] else ""
                overrides = f" (override: {', '.join(record['override_flags'])})" if record["override_flags"] else ""
                print(f"  - {record['source_sha'][:10]} ({record['direction']}): {record['source_subject']}{tags}{overrides}")

    if overdue and args.alert:
        send_alert(
            f"⚠️ WP-562 Ф2: {len(overdue)} расхождений pilot↔production не разобраны дольше {SLA_HOURS}ч. "
            f"Подробности: release-drift-manifest.jsonl"
        )

    sys.exit(3 if overdue else 0)


if __name__ == "__main__":
    main()
