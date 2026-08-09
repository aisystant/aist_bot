"""
Миграция 019: таблица projection_dlq в learning БД (Neon).

Dead-letter queue для stalled projection events.
Circuit breaker + exponential backoff через retry_count / next_retry_at.
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import LEARNING_URL


async def migrate():
    conn = await asyncpg.connect(LEARNING_URL)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS projection_dlq (
                domain         TEXT        NOT NULL,
                event_id       BIGINT      NOT NULL,
                payload        JSONB,
                error          TEXT,
                retry_count    INT         NOT NULL DEFAULT 0,
                next_retry_at  TIMESTAMP WITH TIME ZONE,
                status         TEXT        NOT NULL DEFAULT 'pending',
                created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                PRIMARY KEY (domain, event_id)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_projection_dlq_status_retry
            ON public.projection_dlq (status, next_retry_at)
        """)
        print("Migration 019 complete: projection_dlq created in learning DB.")
    finally:
        await conn.close()


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'projection_dlq' AND table_schema = 'public'
        """)
        if exists:
            return False
    await migrate()
    return True


if __name__ == "__main__":
    asyncio.run(migrate())
