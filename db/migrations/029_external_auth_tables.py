"""
Миграция 029: external_auth_codes + ory_client_tokens в secrets БД (WP-411 Ф2).

Таблицы для бот-медиированной авторизации внешних AI-клиентов (Claude Code MCP):
- external_auth_codes: одноразовые коды (TTL 5 мин), выдаются через /connect_external
- ory_client_tokens: долгосрочные access/refresh-токены внешнего клиента

Таблицы живут в secrets БД (SECRETS_URL / Neon), а не в Railway-local Postgres
(DATABASE_URL), потому что Railway-local user не имеет CREATE ON SCHEMA public.
Все запросы external_clients.py используют get_secrets_pool().

Запуск вручную:
    python -m db.migrations.029_external_auth_tables
PRODSYNC2 (2026-07-10): файл отсутствовал в pilot, хотя таблицы уже существовали
в SECRETS_URL (применено вручную в рамках WP-411 Ф2 разработки/тестирования).
DDL сверен напрямую с прод-версией файла — полное совпадение (колонки, индексы,
constraints). Восстановлен здесь для консистентности кода; при запуске — no-op
(CREATE TABLE IF NOT EXISTS).
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import SECRETS_URL


UP = """
CREATE TABLE IF NOT EXISTS external_auth_codes (
    code TEXT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    account_id UUID NOT NULL,
    scope TEXT NOT NULL DEFAULT 'full',
    created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
    expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eac_expires ON external_auth_codes (expires_at);
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
);
CREATE INDEX IF NOT EXISTS idx_oct_account ON ory_client_tokens (account_id) WHERE revoked_at IS NULL;
"""


async def migrate() -> None:
    conn = await asyncpg.connect(SECRETS_URL)
    try:
        await conn.execute(UP)
        print("Migration 029 complete: external_auth_codes + ory_client_tokens created in secrets DB.")
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
    await migrate()
    print("Migration 029 complete: external_auth_codes + ory_client_tokens created in secrets DB.")
    return True


if __name__ == "__main__":
    asyncio.run(migrate())
