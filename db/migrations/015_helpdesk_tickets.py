"""
Миграция 015: таблица helpdesk_tickets (WP-341 Этап 2).

Хранит маппинг Telegram chat_id ↔ Chatwoot conversation_id
для двустороннего прокси /support.

Запуск:
    python -m db.migrations.015_helpdesk_tickets
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS helpdesk_tickets (
                id               BIGSERIAL PRIMARY KEY,
                chat_id          BIGINT    NOT NULL,
                conversation_id  BIGINT,
                contact_identifier TEXT,
                topic            TEXT      NOT NULL,
                status           TEXT      NOT NULL DEFAULT 'open',
                created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at       TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_helpdesk_chat_id
                ON public.helpdesk_tickets(chat_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_helpdesk_conversation_id
                ON public.helpdesk_tickets(conversation_id)
        """)
        print("Migration 015 complete: helpdesk_tickets created.")
    finally:
        await conn.close()


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'helpdesk_tickets'
        """)
        if exists:
            return False
    await migrate()
    return True


if __name__ == "__main__":
    asyncio.run(migrate())
