#!/usr/bin/env python3
"""
Pre-migration gate for IDCOL1 (WP-7): verify ory_id/dt_user_id agree before
switching the read-path (_SELECT_JOINED in db/queries/users.py) to read
ory_id under the dt_user_id key.

Must be run against prod DATABASE_URL and return zero divergent rows before
this migration is promoted from pilot to new-architecture. If it prints any
rows, do NOT promote — reconcile those users first (db/queries/identity.py
reconcile_dt_user_id) and re-run.

Run:
    DATABASE_URL=<bot_neon_url> python3 scripts/verify_identity_consolidation.py
"""

import asyncio
import os
import sys

import asyncpg


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    pool = await asyncpg.create_pool(db_url, statement_cache_size=0)
    async with pool.acquire() as conn:
        divergent = await conn.fetch(
            """SELECT telegram_id, ory_id, dt_user_id
               FROM public.users
               WHERE ory_id IS NULL
                  OR dt_user_id IS NULL
                  OR ory_id::text != dt_user_id"""
        )

    if not divergent:
        print("OK: 0 divergent rows — safe to switch read-path to ory_id")
        return

    print(f"BLOCK: {len(divergent)} divergent rows found — do NOT promote yet")
    for row in divergent:
        print(f"  telegram_id={row['telegram_id']} ory_id={row['ory_id']} dt_user_id={row['dt_user_id']}")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
