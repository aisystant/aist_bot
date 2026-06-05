"""
Миграция 023: таблица learning.consent_grant (versioned consent, WP-316 Ф9).

Peer-session 2026-06-05-02-bot-error-recurrence-diagnosis (Claude + Kimi):
- Коммит 2f25bcc (18.05) привёз запросы db/queries/consent.py (set_consent_grant /
  get_consent_grant / is_typing_tracking_disabled), НО DDL таблицы не был создан
  нигде — ни в db/models.py, ни в миграциях 001-022.
- Следствие: каждый тап кнопки согласия на анализ текста → asyncpg
  UndefinedTableError (relation "learning.consent_grant" does not exist) →
  классификатор db/L4 → ESCALATION. Детерминированный отказ, не транзиент.
- schema-drift guard (_verify_schema) проверял жёсткий список из 2 таблиц и
  consent_grant в него не входил → рапортовал зелёным при отсутствующей таблице.
  Этой миграцией + добавлением consent_grant в _verify_schema контур замыкается.

Схема выведена из всех обращений в db/queries/consent.py:
  - INSERT (account_id, scope, granted, consent_version, granted_at, interface)
    ON CONFLICT (account_id, scope, consent_version)  → нужен UNIQUE на эту тройку
  - UPDATE granted/revoked_at WHERE account_id AND scope
  - SELECT granted / SELECT id ... ORDER BY granted_at DESC

Запуск:
    python -m db.migrations.023_consent_grant
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import LEARNING_URL


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS learning.consent_grant (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      UUID        NOT NULL,
    scope           TEXT        NOT NULL,
    granted         BOOLEAN     NOT NULL,
    consent_version TEXT        NOT NULL DEFAULT 'v1.0',
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at      TIMESTAMPTZ,
    interface       TEXT,
    UNIQUE (account_id, scope, consent_version)
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_consent_grant_account_scope
ON learning.consent_grant (account_id, scope);
"""


async def migrate():
    conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)
    try:
        async with conn.transaction():
            await conn.execute("CREATE SCHEMA IF NOT EXISTS learning")
            await conn.execute(TABLE_SQL)
            print("  CREATE TABLE learning.consent_grant — OK")
            await conn.execute(INDEX_SQL)
            print("  CREATE INDEX idx_consent_grant_account_scope — OK")
        print("Миграция 023 завершена")
    finally:
        await conn.close()


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('learning.consent_grant')")
        if exists:
            return False
    await migrate()
    return True


if __name__ == "__main__":
    asyncio.run(migrate())
