"""
Миграция 012: Поля предпочтений стиля подачи (WP-151 Ф2).

Добавляет:
  - delivery_format: предпочитаемый формат (examples/tasks/mix)
  - detail_level: уровень детализации (brief/detailed)

Запуск:
    python -m db.migrations.012_delivery_preferences
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Добавить поля delivery_format и detail_level в users."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # delivery_format: examples, tasks, mix (NULL = не спрашивали)
        has_col = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'delivery_format'
        """)
        if has_col == 0:
            await conn.execute("""
                ALTER TABLE public.users
                ADD COLUMN delivery_format TEXT DEFAULT NULL
            """)
            print("Added column: delivery_format")
        else:
            print("Column delivery_format already exists")

        # detail_level: brief, detailed (NULL = не спрашивали)
        has_col = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'detail_level'
        """)
        if has_col == 0:
            await conn.execute("""
                ALTER TABLE public.users
                ADD COLUMN detail_level TEXT DEFAULT NULL
            """)
            print("Added column: detail_level")
        else:
            print("Column detail_level already exists")

        print("Migration 012 complete.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
