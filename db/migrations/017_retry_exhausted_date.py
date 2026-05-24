"""
Миграция 017: колонка retry_exhausted_date в development.user_state.

Хранит дату, когда для пользователя были исчерпаны все retry попытки
доставки контента за текущий день. Предотвращает бесконечный retry-цикл:
без маркера scheduled_check повторно запускает _schedule_retry(attempt=0)
после exhaustion, создавая цепочки 0→1→exhausted→0→...

Сбрасывается ежедневно в 08:00 МСК через _check_schedule_integrity.
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
              AND column_name = 'retry_exhausted_date'
        """)
        if exists:
            return False

    async with pool.acquire() as conn:
        await conn.execute("""
            ALTER TABLE development.user_state
            ADD COLUMN IF NOT EXISTS retry_exhausted_date DATE
        """)
    return True


if __name__ == "__main__":
    from config import DATABASE_URL
    import asyncpg

    async def run():
        pool = await asyncpg.create_pool(DATABASE_URL)
        created = await migrate_if_needed(pool)
        print(f"Migration 017: {'column added' if created else 'already exists'}")
        await pool.close()

    asyncio.run(run())
