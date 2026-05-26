"""
Миграция 018: колонка bot_recheck_at в development.user_state.

Хранит дату следующей проверки статуса бота (разблокировка).
Используется scheduler'ом для отложенной recheck заблокированных пользователей.
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'development'
              AND table_name = 'user_state'
              AND column_name = 'bot_recheck_at'
        """)
        if exists:
            return False

    async with pool.acquire() as conn:
        await conn.execute("""
            ALTER TABLE development.user_state
            ADD COLUMN IF NOT EXISTS bot_recheck_at TIMESTAMP DEFAULT NULL
        """)
    return True


if __name__ == "__main__":
    from config import DATABASE_URL
    import asyncpg

    async def run():
        pool = await asyncpg.create_pool(DATABASE_URL)
        created = await migrate_if_needed(pool)
        print(f"Migration 018: {'column added' if created else 'already exists'}")
        await pool.close()

    asyncio.run(run())
