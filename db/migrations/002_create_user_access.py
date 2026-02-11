"""
Миграция 002: Создание таблицы user_access

Слой доступа и биллинга (DP.AISYS.014 § 4.4).
3 типа: subscription, purchase, feature.

Запуск:
    python -m db.migrations.002_create_user_access
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Создание таблицы user_access"""
    print("Подключение к базе данных...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'user_access'
            )
        """)

        if exists:
            print("Таблица user_access уже существует")
            return

        print("Создание таблицы user_access...")

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_access (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                access_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                expires_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT NOW(),

                CONSTRAINT uq_user_access UNIQUE (user_id, access_type, resource_id)
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_access_user_id
            ON user_access(user_id)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_access_resource
            ON user_access(resource_id)
        ''')

        print("Таблица user_access успешно создана")

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(migrate())
