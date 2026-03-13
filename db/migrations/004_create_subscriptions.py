"""
Миграция 004: Создание таблицы subscriptions + trial_started_at.

Подписки через Telegram Stars (DP.AISYS.014 § 4.4, РП #9).

Запуск:
    python -m db.migrations.004_create_subscriptions
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Создание таблицы subscriptions и колонки trial_started_at."""
    print("Подключение к базе данных...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        # --- subscriptions table ---
        exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'subscriptions'
            )
        """)

        if exists:
            print("Таблица subscriptions уже существует")
        else:
            print("Создание таблицы subscriptions...")

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    telegram_payment_charge_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    stars_amount INTEGER NOT NULL,
                    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMP NOT NULL,
                    cancelled_at TIMESTAMP DEFAULT NULL,
                    is_first_recurring BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')

            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_subscriptions_chat_id
                ON subscriptions(chat_id)
            ''')

            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_subscriptions_active
                ON subscriptions(chat_id, status)
            ''')

            print("Таблица subscriptions успешно создана")

        # --- trial_started_at column (now in development.user_state) ---
        col_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.columns
                WHERE table_schema = 'development'
                  AND table_name = 'user_state'
                  AND column_name = 'trial_started_at'
            )
        """)

        if col_exists:
            print("Колонка trial_started_at уже существует в user_state")
        else:
            print("Добавление колонки trial_started_at в user_state...")
            await conn.execute(
                'ALTER TABLE development.user_state ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMP DEFAULT NULL'
            )
            print("Колонка trial_started_at добавлена")

        # --- Backfill: trial_started_at для существующих пользователей ---
        updated = await conn.execute('''
            UPDATE development.user_state s
            SET trial_started_at = u.created_at
            FROM public.users u
            WHERE u.telegram_id = s.chat_id
              AND s.trial_started_at IS NULL
              AND s.onboarding_completed = TRUE
        ''')
        print(f"Backfill trial_started_at → created_at: {updated}")

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(migrate())
