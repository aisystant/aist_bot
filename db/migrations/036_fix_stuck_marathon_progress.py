"""
Migration 036: fix marathon_progress for users whose lesson_practice queue items
reached status='failed' due to IndeterminateDatatypeError in update_progress
(bug fixed in commit 72df544, 2026-06-29).

Root cause: _update_sql numbers columns starting at $2, but update_progress was
placing user_id last and using a dynamic WHERE index — leaving $1 unreferenced,
so PostgreSQL (Neon pgbouncer, statement_cache_size=0) returned OID=0 and raised
IndeterminateDatatypeError on every MarathonQueue run for affected users.

Effect: bot.send_message succeeded (lesson delivered), mark_queue_sent ran, then
update_progress failed, then schedule_queue_retry reset status back to 'pending'.
After 3 total attempts mark_queue_failed was called. Result: users received the
lesson 3 times but current_day was never advanced.

This migration:
1. Finds users with status='failed' lesson_practice queue items whose
   current_day in marathon_progress still matches the failed day_number.
2. Marks those queue items as 'sent' (content was already delivered).
3. Advances current_day by 1 (what update_progress would have written).

Idempotent: the JOIN condition (p.current_day = q.day_number) ensures no-op
if current_day was already manually corrected.

Manual run:
    LEARNING_DATABASE_URL=<dsn> python -m db.migrations.036_fix_stuck_marathon_progress
"""

import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


async def _fix(conn) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT DISTINCT q.user_id, q.day_number
        FROM learning.marathon_queue q
        JOIN learning.marathon_progress p ON p.user_id = q.user_id
        WHERE q.status = 'failed'
          AND q.content_type = 'lesson_practice'
          AND p.current_day = q.day_number
        """
    )
    if not rows:
        return []

    fixed = []
    for row in rows:
        uid, day = row["user_id"], row["day_number"]
        await conn.execute(
            """
            UPDATE learning.marathon_queue
            SET status = 'sent', updated_at = NOW()
            WHERE user_id = $1
              AND day_number = $2
              AND status = 'failed'
              AND content_type = 'lesson_practice'
            """,
            uid, day,
        )
        await conn.execute(
            """
            UPDATE learning.marathon_progress
            SET current_day = current_day + 1, updated_at = NOW()
            WHERE user_id = $1
              AND current_day = $2
            """,
            uid, day,
        )
        fixed.append({"user_id": uid, "day_number": day})
    return fixed


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        fixed = await _fix(conn)
    if fixed:
        for item in fixed:
            print(f"  fixed user_id={item['user_id']} day={item['day_number']}: "
                  f"queue→sent, current_day+1")
    return bool(fixed)


if __name__ == "__main__":
    from config import LEARNING_URL

    async def run():
        pool = await asyncpg.create_pool(LEARNING_URL, statement_cache_size=0)
        fixed = await migrate_if_needed(pool)
        print(f"Migration 036: {'fixed ' + str(len(fixed)) + ' user(s)' if fixed else 'no stuck users found'}")
        await pool.close()

    asyncio.run(run())
