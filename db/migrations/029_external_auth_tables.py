"""
Миграция 029: external_auth_codes + ory_client_tokens в основной БД (WP-411 Ф2).

Таблицы для бот-медиированной авторизации внешних AI-клиентов (Claude Code MCP):
- external_auth_codes: одноразовые коды (TTL 5 мин), выдаются через /connect_external
- ory_client_tokens: долгосрочные access/refresh-токены внешнего клиента

Причина миграции: SKIP_DB_MIGRATIONS=true на проде пропускает create_tables(),
поэтому таблицы не создавались при деплое Ф2.

Запуск вручную:
    python -m db.migrations.029_external_auth_tables
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def _create_tables(conn) -> None:
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS external_auth_codes (
            code TEXT PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            account_id UUID NOT NULL,
            scope TEXT NOT NULL DEFAULT 'full',
            created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
            expires_at TIMESTAMP NOT NULL
        )
    ''')
    await conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_eac_expires
        ON external_auth_codes (expires_at)
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS ory_client_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id UUID NOT NULL,
            access_token_hash TEXT NOT NULL UNIQUE,
            refresh_token_hash TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL DEFAULT 'full',
            client_label TEXT DEFAULT 'Claude Code',
            last_used TIMESTAMP,
            revoked_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
        )
    ''')
    await conn.execute('''
        ALTER TABLE ory_client_tokens
        DROP COLUMN IF EXISTS access_token_enc,
        DROP COLUMN IF EXISTS refresh_token_enc
    ''')
    await conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_oct_account
        ON ory_client_tokens (account_id)
        WHERE revoked_at IS NULL
    ''')


async def migrate():
    conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
    try:
        await _create_tables(conn)
        print("Migration 029 complete: external_auth_codes + ory_client_tokens created.")
    finally:
        await conn.close()


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'external_auth_codes' AND table_schema = 'public'
        """)
        if exists:
            return False
        # Use pool connection directly — avoids permission issues with direct connect
        await _create_tables(conn)
    print("Migration 029 complete: external_auth_codes + ory_client_tokens created.")
    return True


if __name__ == "__main__":
    asyncio.run(migrate())
