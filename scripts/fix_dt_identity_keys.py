#!/usr/bin/env python3
"""Merge duplicate users.id twins into their canonical ory_id twins.

Only 2_collected is migrated; existing target groups take precedence. Other
target categories are preserved. A source containing other categories is
refused rather than deleted. The complete repair commits or rolls back together.

Usage: DATABASE_URL=<bot_data_url> python3 scripts/fix_dt_identity_keys.py [--dry-run]

This is a maintenance operation: stop writers using legacy identity keys before
running it. Row locks protect this transaction, not later recreation of old keys.
"""

import argparse
import asyncio
import json
import os
import sys

import asyncpg


class UnsafeRepairError(ValueError):
    """The documented 2_collected repair cannot safely handle these rows."""


_PAIRS_SQL = """
    SELECT u.id::text AS source_id, u.ory_id::text AS target_id
    FROM public.users u
    JOIN public.digital_twins source ON source.user_id = u.id::text
    JOIN public.digital_twins target ON target.user_id = u.ory_id::text
    WHERE u.ory_id IS NOT NULL AND u.id::text <> u.ory_id::text
    ORDER BY u.id
"""


def _validate_pairs(pairs) -> list[str]:
    sources = [pair["source_id"] for pair in pairs]
    targets = [pair["target_id"] for pair in pairs]
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
        raise UnsafeRepairError("Identity mapping is not one-to-one; no rows changed")
    if set(sources) & set(targets):
        raise UnsafeRepairError(
            "Source and target identity sets overlap; no rows changed"
        )
    return sorted(set(sources + targets))


def _document(raw) -> dict:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise UnsafeRepairError("Twin data must be a JSON object; no rows changed")
    if "2_collected" in value and not isinstance(value["2_collected"], dict):
        raise UnsafeRepairError("2_collected must be a JSON object; no rows changed")
    return value


async def _lock_documents(conn, keys: list[str]) -> dict[str, dict]:
    rows = await conn.fetch(
        """
        SELECT user_id, data FROM public.digital_twins
        WHERE user_id = ANY($1::text[])
        ORDER BY user_id
        FOR UPDATE
        """,
        keys,
    )
    if len(rows) != len(keys):
        raise UnsafeRepairError("Identity rows changed during repair; no rows changed")
    return {row["user_id"]: _document(row["data"]) for row in rows}


async def _merge_pair(conn, source_id: str, target_id: str) -> None:
    merged = await conn.fetchval(
        """
        UPDATE public.digital_twins AS target
        SET data = jsonb_set(
                target.data, '{2_collected}',
                COALESCE(source.data->'2_collected', '{}'::jsonb)
                    || COALESCE(target.data->'2_collected', '{}'::jsonb),
                true
            ),
            updated_at = NOW()
        FROM public.digital_twins AS source
        WHERE source.user_id = $1 AND target.user_id = $2
        RETURNING target.user_id
        """,
        source_id,
        target_id,
    )
    if merged != target_id:
        raise UnsafeRepairError("Expected target was not updated; repair rolled back")
    deleted = await conn.fetchval(
        "DELETE FROM public.digital_twins WHERE user_id = $1 RETURNING user_id",
        source_id,
    )
    if deleted != source_id:
        raise UnsafeRepairError("Expected source was not removed; repair rolled back")


async def repair_identity_keys(conn) -> int:
    """Apply only validated pairs, with stable identities and locked documents."""
    async with conn.transaction(isolation="repeatable_read"):
        pairs = await conn.fetch(_PAIRS_SQL + " FOR UPDATE OF u")
        keys = _validate_pairs(pairs)
        if not keys:
            return 0
        documents = await _lock_documents(conn, keys)
        for pair in pairs:
            if set(documents[pair["source_id"]]) - {"2_collected"}:
                raise UnsafeRepairError(
                    "Source contains categories outside 2_collected; repair rolled back"
                )
        for pair in pairs:
            await _merge_pair(conn, pair["source_id"], pair["target_id"])
        return len(pairs)


async def main(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")
    conn = await asyncpg.connect(db_url)
    try:
        if dry_run:
            pairs = await conn.fetch(_PAIRS_SQL)
            _validate_pairs(pairs)
            print(f"[DRY RUN] Identity pairs: {len(pairs)}; no changes applied.")
            print("Document safety is rechecked under locks when applying the repair.")
            return
        repaired = await repair_identity_keys(conn)
        remaining = await conn.fetch(_PAIRS_SQL)
        print(f"Repaired pairs: {repaired}; remaining identity pairs: {len(remaining)}")
        if remaining:
            print(
                "Legacy identity writers may still be active; inspect before retrying."
            )
    except UnsafeRepairError as exc:
        print(f"Repair refused: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix digital_twins identity key duplicates"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pair count without applying changes",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
