"""
Миграция 003: Создание таблицы service_usage

Аналитика использования сервисов (DP.AISYS.014 § 4.5).
Данные для адаптивной сортировки меню.

Запуск:
    python -m db.migrations.003_create_service_usage
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Создание таблицы service_usage"""
    print("Подключение к базе данных...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'service_usage'
            )
        """)

        if exists:
            print("Таблица service_usage уже существует")
            return

        print("Создание таблицы service_usage...")

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS service_usage (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                service_id TEXT NOT NULL,
                action TEXT DEFAULT 'enter',
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_service_usage_user
            ON service_usage(user_id)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_service_usage_service
            ON service_usage(user_id, service_id)
        ''')

        print("Таблица service_usage успешно создана")

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(migrate())
