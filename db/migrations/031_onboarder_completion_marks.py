"""
Миграция 031: отметки завершения характеристик Онбордера в user_state.
(номер 030 был занят 030_computed_tier.py — коллизия разведена 2026-06-11).

# see DP.SC.170, DP.ROLE.067
# (WP-406 Ф5, peer-session 2026-06-11-07-wp406-onboarder-f5-impl)

Две nullable-колонки в development.user_state:
  x2_completed_at — когда закрыто понимание сообщества (Х2);
  x3_completed_at — когда закрыта траектория (Х3: ступень + узкое место + курс).

Индексируемые отметки (не флаги в current_context) — нужны для проактивных
напоминаний застрявшим и аналитики флота (Ф6) без backfill. NULL = не закрыто.
Колонки также добавлены в CREATE TABLE и inline-ALTER в db/models.py (применяется
на старте бота, как notify_nudges). Этот файл — для ручного/документированного
запуска и idempotent-проверки на существующей БД.

Запуск:
    python -m db.migrations.030_onboarder_completion_marks
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_COLUMNS = ("x2_completed_at", "x3_completed_at")


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        present = await conn.fetchval(
            """
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'development'
              AND table_name = 'user_state'
              AND column_name = ANY($1::text[])
            """,
            list(_COLUMNS),
        )
        if present == len(_COLUMNS):
            return False

    async with pool.acquire() as conn:
        await conn.execute(
            """
            ALTER TABLE development.user_state
                ADD COLUMN IF NOT EXISTS x2_completed_at TIMESTAMP DEFAULT NULL,
                ADD COLUMN IF NOT EXISTS x3_completed_at TIMESTAMP DEFAULT NULL
            """
        )
    return True


if __name__ == "__main__":
    from config import DATABASE_URL

    async def run():
        pool = await asyncpg.create_pool(DATABASE_URL)
        created = await migrate_if_needed(pool)
        print(f"Migration 031: {'columns added' if created else 'already present'}")
        await pool.close()

    asyncio.run(run())
