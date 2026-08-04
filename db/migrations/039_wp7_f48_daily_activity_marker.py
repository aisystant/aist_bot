"""
Migration 039: development.daily_activity_marker — atomic per-day gate for the
active-days counter (WP-7 Ф48, peer-session 2026-08-04-27-wp7-f48-bot-reliability).

Root cause: record_active_day() gated its counter UPDATE on last_active_date,
but last_active_date is also written unconditionally by touch_last_active_date()
(core/middleware.py, fire-and-forget on every interaction). The two writers race —
if touch wins, the counter increment silently no-ops even on a genuine new active
day. daily_activity_marker replaces last_active_date as that gate: it lives in the
same database as development.user_state, so record_active_day() can INSERT the
marker and UPDATE the counters inside one transaction — real atomicity, not by name.

Also added, in db/models.py CREATE TABLE (applied automatically on bot startup,
same pattern as x2_completed_at/x3_completed_at in migration 031). This file is
the manual/documented run for an existing database.

Run:
    python -m db.migrations.039_wp7_f48_daily_activity_marker
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
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'development' AND table_name = 'daily_activity_marker'
            )
            """
        )
        if exists:
            return False

        await conn.execute(
            """
            CREATE TABLE development.daily_activity_marker (
                chat_id BIGINT NOT NULL,
                activity_date DATE NOT NULL,
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                PRIMARY KEY (chat_id, activity_date)
            )
            """
        )
    return True


if __name__ == "__main__":
    from config import DATABASE_URL

    async def run():
        pool = await asyncpg.create_pool(DATABASE_URL)
        created = await migrate_if_needed(pool)
        print(f"Migration 039: {'table created' if created else 'already present'}")
        await pool.close()

    asyncio.run(run())
