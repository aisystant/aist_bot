"""
Миграция 009: Таблицы для Сообщества IWE (WP-181).

Создаёт:
  - workshop_payments: оплаты семинара (по telegram_id, без привязки к Aisystant)
  - community_members: логирование участников чатов Сообщества/Мастерской

Связь: WP-181 ТЗ §6.1 (А, Е), PROCESSES.md.

Запуск:
    python -m db.migrations.009_workshop_community
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Создать таблицы workshop_payments и community_members."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Проверяем, не применена ли уже миграция
        has_table = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'workshop_payments'
        """)
        if has_table > 0:
            print("Migration 009 already applied, skipping.")
            return

        print("Applying migration 009: workshop_payments + community_members...")

        # 1. workshop_payments — оплаты семинара
        await conn.execute("""
            CREATE TABLE workshop_payments (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                aisystant_id TEXT,
                amount NUMERIC NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT NOT NULL DEFAULT 'bot',
                payment_id TEXT,
                paid_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        await conn.execute("""
            CREATE INDEX idx_workshop_payments_tg
                ON workshop_payments (telegram_id);
            CREATE INDEX idx_workshop_payments_status
                ON workshop_payments (telegram_id, status)
                WHERE status = 'success';
            CREATE UNIQUE INDEX idx_workshop_payments_payment_id
                ON workshop_payments (payment_id)
                WHERE payment_id IS NOT NULL;
        """)

        # 2. community_members — логирование участников
        await conn.execute("""
            CREATE TABLE community_members (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                username TEXT,
                first_name TEXT,
                source TEXT NOT NULL DEFAULT 'unknown',
                joined_at TIMESTAMP DEFAULT NOW(),
                left_at TIMESTAMP
            );
        """)

        await conn.execute("""
            CREATE INDEX idx_community_members_chat
                ON community_members (chat_id)
                WHERE left_at IS NULL;
            CREATE UNIQUE INDEX idx_community_members_unique
                ON community_members (telegram_id, chat_id)
                WHERE left_at IS NULL;
        """)

        print("Migration 009 applied successfully.")
        print("  Tables: workshop_payments, community_members")
        print("  Indexes: tg, status, chat, unique(tg+chat)")

    finally:
        await conn.close()


async def rollback():
    """Откат миграции 009."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        print("Rolling back migration 009...")
        await conn.execute("DROP TABLE IF EXISTS community_members;")
        await conn.execute("DROP TABLE IF EXISTS workshop_payments;")
        print("Rollback 009 done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--rollback":
        asyncio.run(rollback())
    else:
        asyncio.run(migrate())
