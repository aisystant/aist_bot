"""
Migration 037: scheduled_post deduplication + atomic publish lock (WP-167).

Root cause of duplicate club posts (2026-06-29):
1. _smart_publisher_scan could insert the same source_file twice when two scans
   overlapped (startup scan + cron, or multiple bot replicas).
2. _discourse_scheduled_publish selected all pending rows, then published,
   then marked done — a race window allowed two job instances to pick up the
   same row.

This migration adds DB-level guards:
- CHECK constraint on scheduled_post.status
- Partial unique index on (chat_id, source_file) for pending/publishing rows,
  preventing duplicate scheduling of the same source file.
- Status value 'publishing' used for atomic row lock via
  SELECT ... FOR UPDATE SKIP LOCKED + UPDATE status='publishing'.

Idempotent: uses IF NOT EXISTS / DROP+ADD pattern.

Manual run:
    PUBLICATION_URL=<dsn> python -m db.migrations.037_scheduled_post_dedup_lock
"""

import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


_STATUS_VALUES = ("pending", "publishing", "published", "failed")


async def _ensure_status_check(conn: asyncpg.Connection) -> None:
    """Add CHECK constraint for scheduled_post.status if absent."""
    row = await conn.fetchrow(
        """
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'scheduled_post'::regclass
          AND conname = 'chk_scheduled_post_status'
        """
    )
    if row:
        return
    await conn.execute(
        """
        ALTER TABLE scheduled_post
        ADD CONSTRAINT chk_scheduled_post_status
        CHECK (status IN ('pending', 'publishing', 'published', 'failed'))
        """
    )


async def _deduplicate_pending_source_files(conn: asyncpg.Connection) -> int:
    """Remove duplicate pending/publishing rows with the same (chat_id, source_file).

    Keeps the row with the earliest schedule_time. Required before creating the
    partial unique index.
    """
    result = await conn.execute(
        """
        DELETE FROM scheduled_post
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY chat_id, source_file
                           ORDER BY schedule_time, id
                       ) AS rn
                FROM scheduled_post
                WHERE status IN ('pending', 'publishing')
                  AND source_file IS NOT NULL
            ) sub
            WHERE rn > 1
        )
        """
    )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def _ensure_unique_pending_index(conn: asyncpg.Connection) -> None:
    """Create partial unique index preventing duplicate pending/publishing rows."""
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_post_unique_pending_source
        ON scheduled_post (chat_id, source_file)
        WHERE status IN ('pending', 'publishing') AND source_file IS NOT NULL
        """
    )


async def _ensure_updated_at(conn: asyncpg.Connection) -> None:
    """Add updated_at column used for stale publishing detection."""
    await conn.execute(
        """
        ALTER TABLE scheduled_post
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()
        """
    )


async def _backfill_publishing_to_pending(conn: asyncpg.Connection) -> int:
    """Recover rows left in 'publishing' after a previous crash."""
    result = await conn.execute(
        """
        UPDATE scheduled_post
        SET status = 'pending'
        WHERE status = 'publishing'
        """
    )
    # asyncpg returns e.g. 'UPDATE 3'
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        await _ensure_updated_at(conn)
        await _ensure_status_check(conn)
        removed = await _deduplicate_pending_source_files(conn)
        await _ensure_unique_pending_index(conn)
        recovered = await _backfill_publishing_to_pending(conn)
    if removed:
        print(f"  removed {removed} duplicate pending/publishing row(s)")
    if recovered:
        print(f"  recovered {recovered} row(s) from 'publishing' to 'pending'")
    return True


if __name__ == "__main__":
    from config import PUBLICATION_URL

    async def run():
        if not PUBLICATION_URL:
            print("PUBLICATION_URL not set")
            sys.exit(1)
        pool = await asyncpg.create_pool(PUBLICATION_URL, statement_cache_size=0)
        try:
            await migrate_if_needed(pool)
            print("Migration 037: scheduled_post dedup lock ready")
        finally:
            await pool.close()

    asyncio.run(run())
