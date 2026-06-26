"""
Migration 034: add expires_at to public.redeemed_events.

# see WP-446 Ф2 (provisional reserve TTL for subscription bonus redemption)

Adds nullable TIMESTAMPTZ column used as an explicit TTL for provisional reserves:
- provisional yk_{uuid} reserve: expires_at = NOW() + 1h (crash-safety)
- after update_reserve_payment_id: expires_at = NOW() + 7 days (prevents 30-min rollback)
- regular reserves (non-provisional): expires_at = NULL → rollback falls back to reserved_at + 30min

rollback_expired_reservations uses COALESCE(expires_at, reserved_at + interval '30 min') < NOW()
so existing rows (expires_at NULL) behave exactly as before.

Manual run:
    REWARDS_URL=<dsn> python -m db.migrations.034_redeemed_events_expires_at
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
                  AND column_name = 'expires_at'
            )
            """
        )
        if exists:
            return False

    async with pool.acquire() as conn:
        await conn.execute(
            """
            ALTER TABLE public.redeemed_events
            ADD COLUMN expires_at TIMESTAMPTZ
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
        print(f"Migration 034: {'expires_at added to redeemed_events' if created else 'already exists'}")
        await pool.close()

    asyncio.run(run())
