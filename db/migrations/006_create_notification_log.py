"""
Миграция 006: Создать таблицу notification_log (WP-152).

Единая таблица идемпотентности уведомлений.
Заменяет 6+ разрозненных guard-ов: reminders.sent, notification_sent_at,
nudge_log, milestone dedup, trial expiry, feed digest.

Запуск:
    python -m db.migrations.006_create_notification_log
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Создать таблицу notification_log."""
    print("Подключение к базе данных...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        # Создать таблицу
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS notification_log (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                notification_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload JSONB DEFAULT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(idempotency_key)
            )
        ''')
        print("✅ Таблица notification_log создана")

        # Индексы
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_notification_log_chat_type
            ON notification_log(chat_id, notification_type)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_notification_log_created
            ON notification_log(created_at)
        ''')
        print("✅ Индексы созданы")

        # Проверка
        count = await conn.fetchval('SELECT COUNT(*) FROM notification_log')
        print(f"✅ notification_log: {count} записей")

    finally:
        await conn.close()
    print("Миграция 006 завершена.")


if __name__ == '__main__':
    asyncio.run(migrate())
