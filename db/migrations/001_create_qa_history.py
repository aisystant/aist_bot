"""
Миграция 001: Создание таблицы qa_history

Запуск:
    python -m db.migrations.001_create_qa_history
"""

import asyncio
import asyncpg
import os
import sys

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Создание таблицы qa_history"""
    print("Подключение к базе данных...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        # Проверяем существует ли таблица
        exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'qa_history'
            )
        """)

        if exists:
            print("Таблица qa_history уже существует")
            return

        print("Создание таблицы qa_history...")

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS qa_history (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,

                mode TEXT,
                context_topic TEXT,

                question TEXT,
                answer TEXT,
                mcp_sources TEXT DEFAULT '[]',

                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Создаем индекс для быстрого поиска по chat_id
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_qa_history_chat_id
            ON qa_history(chat_id)
        ''')

        print("Таблица qa_history успешно создана")

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(migrate())
