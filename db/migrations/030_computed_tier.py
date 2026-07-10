"""
Миграция 030: computed_tier + chat_id для ory_client_tokens (WP-411 Ф4).

computed_tier — тир пользователя на момент выдачи/обновления ict_-токена.
Single source of truth по тиру для внешних клиентов через introspect.
chat_id — Telegram chat_id, нужен при refresh для пересчёта тира.

Секция UP идемпотентна (IF NOT EXISTS).
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import SECRETS_URL

UP = """
ALTER TABLE ory_client_tokens ADD COLUMN IF NOT EXISTS computed_tier TEXT;
ALTER TABLE ory_client_tokens ADD COLUMN IF NOT EXISTS chat_id BIGINT;
"""


async def migrate() -> None:
    conn = await asyncpg.connect(SECRETS_URL)
    try:
        await conn.execute(UP)
        print("Migration 030 complete: computed_tier + chat_id added to ory_client_tokens.")
    finally:
        await conn.close()


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'ory_client_tokens'
              AND column_name = 'computed_tier'
              AND table_schema = 'public'
        """)
        if exists:
            return False
    await migrate()
    return True


if __name__ == "__main__":
    asyncio.run(migrate())
