#!/usr/bin/env python3
"""
Backfill public.users.dt_user_id for users who did /link but never went through DT OAuth.

Problem: save_aisystant_link() wrote aisystant_id but not dt_user_id (Ory UUID).
Any handler checking intern.get('dt_user_id') (/points, /progress, /referral, etc.)
returned "not linked" even though aisystant_id was set.

Fix: read Ory UUID from persona.ory_identity.account_id and write to public.users.dt_user_id.

Run:
    DATABASE_URL=<bot_neon_url> PERSONA_URL=<persona_neon_url> \\
        python3 scripts/backfill_dt_user_id.py [--dry-run]
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

import asyncpg


async def main(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    persona_url = os.environ.get("PERSONA_URL")
    if not db_url or not persona_url:
        print("ERROR: DATABASE_URL and PERSONA_URL are required", file=sys.stderr)
        sys.exit(1)

    bot_pool = await asyncpg.create_pool(db_url, statement_cache_size=0)
    persona_pool = await asyncpg.create_pool(persona_url, statement_cache_size=0)

    async with bot_pool.acquire() as bot_conn, persona_pool.acquire() as pconn:
        candidates = await bot_conn.fetch(
            """SELECT telegram_id, aisystant_id
               FROM public.users
               WHERE aisystant_id IS NOT NULL AND dt_user_id IS NULL"""
        )
        print(f"Candidates: {len(candidates)} users with aisystant_id but no dt_user_id")

        updated = 0
        skipped = 0
        for row in candidates:
            chat_id = row["telegram_id"]
            aisystant_id = str(row["aisystant_id"])

            ory_uuid = await pconn.fetchval(
                """SELECT account_id FROM public.ory_identity
                   WHERE (traits->>'aisystant_suser_id' = $1 OR traits->>'aisystant_id' = $1)
                   ORDER BY (traits->>'aisystant_suser_id' = $1) DESC
                   LIMIT 1""",
                aisystant_id,
            )

            if not ory_uuid:
                print(f"  SKIP chat_id={chat_id} aisystant_id={aisystant_id}: no ory_identity row")
                skipped += 1
                continue

            print(f"  UPDATE chat_id={chat_id} aisystant_id={aisystant_id} → dt_user_id={ory_uuid}", end="")

            if not dry_run:
                await bot_conn.execute(
                    """UPDATE public.users
                       SET dt_user_id = $2, updated_at = $3
                       WHERE telegram_id = $1 AND dt_user_id IS NULL""",
                    chat_id, str(ory_uuid), datetime.utcnow(),
                )
                print(" ✓")
            else:
                print(" (dry-run)")

            updated += 1

    await bot_pool.close()
    await persona_pool.close()

    print(f"\nDone: {updated} updated, {skipped} skipped (no ory_identity)")
    if dry_run:
        print("Re-run without --dry-run to apply.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
