"""
Migration 035: add product_code to public.redeemed_events.

# see WP-446 Ф3a (course bonus redemption — product_code scopes confirm to a specific course)

Adds nullable TEXT column so confirm_course_reserves can match reserves to the exact
course that was paid, rather than confirming for any course the user has access to.

Manual run:
    REWARDS_URL=<dsn> python -m db.migrations.035_redeemed_events_product_code
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'redeemed_events'
                  AND column_name = 'product_code'
            )
            """
        )
        if exists:
            return False

    async with pool.acquire() as conn:
        await conn.execute(
            """
            ALTER TABLE public.redeemed_events
            ADD COLUMN product_code TEXT
            """
        )
    return True


if __name__ == "__main__":
    dsn = os.environ.get("REWARDS_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("Error: REWARDS_URL not set", file=sys.stderr)
        sys.exit(1)

    async def run():
        pool = await asyncpg.create_pool(dsn)
        created = await migrate_if_needed(pool)
        print(f"Migration 035: {'product_code added to redeemed_events' if created else 'already exists'}")
        await pool.close()

    asyncio.run(run())
