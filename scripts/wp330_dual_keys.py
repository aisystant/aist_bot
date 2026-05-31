"""WP-330 C9a — JSON dual keys: sync from mirror + add long_complex aliases.

What it does:
  1. Reads DS-marathon-v2-tseren mirror (source-of-truth).
  2. Reads aist_bot data/marathon-content.json (prod-served).
  3. For each day, copies missing keys from mirror to bot:
     lesson_short_simple, practice_short_simple,
     lesson_short_complex, practice_short_complex,
     lesson_long_simple, practice_long_simple.
  4. In BOTH bot and mirror: creates dual-key
     lesson_long_complex := lesson_full
     practice_long_complex := practice_full
     (legacy lesson_full/practice_full retained for prod backward-compat).
  5. md5 sync verification.

Idempotent: re-running yields zero diff.
Run from repo root: python3 scripts/wp330_dual_keys.py
"""
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOT_JSON = REPO_ROOT / "data" / "marathon-content.json"
MIRROR_JSON = REPO_ROOT.parents[1] / "DS-marathon-v2-tseren" / "materials" / "participants" / "marathon-content.json"

SYNC_KEYS = [
    "lesson_short_simple", "practice_short_simple",
    "lesson_short_complex", "practice_short_complex",
    "lesson_long_simple", "practice_long_simple",
]
DUAL_KEY_PAIRS = [
    ("lesson_full", "lesson_long_complex"),
    ("practice_full", "practice_long_complex"),
]


def _file_md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _write_json(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def sync_and_dual(json_path: Path, mirror_data: dict, add_synced_keys: bool) -> dict:
    """Apply sync + dual-keys to JSON at json_path. Returns stats dict."""
    with json_path.open(encoding="utf-8") as f:
        data = json.load(f)

    days = data.get("days", {})
    mirror_days = mirror_data.get("days", {})

    stats = {"days_processed": 0, "synced_keys": 0, "dual_keys_added": 0}

    for day_num in sorted(days.keys(), key=int):
        day_data = days[day_num]
        mirror_day = mirror_days.get(day_num, {})

        if add_synced_keys:
            for key in SYNC_KEYS:
                if key not in day_data and key in mirror_day:
                    day_data[key] = mirror_day[key]
                    stats["synced_keys"] += 1

        for src_key, dst_key in DUAL_KEY_PAIRS:
            if dst_key not in day_data and src_key in day_data:
                day_data[dst_key] = day_data[src_key]
                stats["dual_keys_added"] += 1

        stats["days_processed"] += 1

    _write_json(json_path, data)
    return stats


def main() -> int:
    if not BOT_JSON.exists():
        print(f"ERROR: bot JSON missing: {BOT_JSON}", file=sys.stderr)
        return 1
    if not MIRROR_JSON.exists():
        print(f"ERROR: mirror JSON missing: {MIRROR_JSON}", file=sys.stderr)
        return 1

    with MIRROR_JSON.open(encoding="utf-8") as f:
        mirror_data = json.load(f)

    bot_stats = sync_and_dual(BOT_JSON, mirror_data, add_synced_keys=True)
    mirror_stats = sync_and_dual(MIRROR_JSON, mirror_data, add_synced_keys=False)

    print(f"BOT: {bot_stats}")
    print(f"MIRROR: {mirror_stats}")
    print(f"BOT md5: {_file_md5(BOT_JSON)}")
    print(f"MIRROR md5: {_file_md5(MIRROR_JSON)}")

    with BOT_JSON.open(encoding="utf-8") as f:
        bot_after = json.load(f)
    with MIRROR_JSON.open(encoding="utf-8") as f:
        mirror_after = json.load(f)

    expected_keys = SYNC_KEYS + [pair[1] for pair in DUAL_KEY_PAIRS]
    for day_num in sorted(bot_after["days"].keys(), key=int):
        d = bot_after["days"][day_num]
        missing = [k for k in expected_keys if k not in d]
        if missing:
            print(f"BOT day {day_num} missing: {missing}", file=sys.stderr)
            return 2

    for day_num in sorted(mirror_after["days"].keys(), key=int):
        d = mirror_after["days"][day_num]
        missing = [k for k in [pair[1] for pair in DUAL_KEY_PAIRS] if k not in d]
        if missing:
            print(f"MIRROR day {day_num} missing dual: {missing}", file=sys.stderr)
            return 2

    print("OK: all 14 days have full key set; bot + mirror dual-key sync complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
