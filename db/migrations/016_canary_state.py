"""
Миграция 016: таблица canary_state в learning БД (Neon).

Хранит последнее время паузы планировщика из-за деградации Claude API.
Позволяет восстанавливать состояние канарейки после редеплоя.
Единственная строка: id=1 (UPSERT-only).
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
            CREATE TABLE IF NOT EXISTS canary_state (
                id           SMALLINT  PRIMARY KEY DEFAULT 1,
                paused_until TIMESTAMP,
                updated_at   TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT canary_state_single_row CHECK (id = 1)
            )
        """)
        print("Migration 016 complete: canary_state created in learning DB.")
    finally:
        await conn.close()


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'canary_state' AND table_schema = 'public'
        """)
        if exists:
            return False
    await migrate()
    return True


if __name__ == "__main__":
    asyncio.run(migrate())
