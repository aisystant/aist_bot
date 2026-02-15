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

        # --- trial_started_at column ---
        col_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.columns
                WHERE table_name = 'interns' AND column_name = 'trial_started_at'
            )
        """)

        if col_exists:
            print("Колонка trial_started_at уже существует")
        else:
            print("Добавление колонки trial_started_at...")
            await conn.execute(
                'ALTER TABLE interns ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMP DEFAULT NULL'
            )
            print("Колонка trial_started_at добавлена")

        # --- Backfill: trial_started_at для существующих пользователей ---
        # Все зарегистрированные до запуска получают триал с даты запуска (23 фев)
        from config.settings import SUBSCRIPTION_LAUNCH_DATE
        updated = await conn.execute('''
            UPDATE interns
            SET trial_started_at = $1
            WHERE trial_started_at IS NULL
              AND onboarding_completed = TRUE
        ''', SUBSCRIPTION_LAUNCH_DATE)
        print(f"Backfill trial_started_at → {SUBSCRIPTION_LAUNCH_DATE}: {updated}")

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(migrate())
