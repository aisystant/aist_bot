"""
Миграция 023: таблица learning.consent_grant (versioned consent, WP-316 Ф9).

Peer-session 2026-06-05-02-bot-error-recurrence-diagnosis (Claude + Kimi):
- Коммит 2f25bcc (18.05) привёз запросы db/queries/consent.py (set/get/is_typing_*),
  но DDL таблицы не был создан нигде в коде (ни db/models.py, ни миграции 001-022).
- Следствие: тап кнопки согласия → set_consent_grant INSERT ... ON CONFLICT падал
  → классификатор db/L4 → ESCALATION.

ВАЖНО (v2, тот же peer-session): на pilot и prod таблица УЖЕ существовала на момент
деплоя (migrate_if_needed-no-op, лога "created" не было). Значит `CREATE TABLE IF NOT
EXISTS` сам по себе НЕ гарантирует наличие UNIQUE-таргета для ON CONFLICT, если таблица
была создана раньше (вручную / частично) без него. Поэтому миграция сделана идемпотентной
ensure-схемой, которая выполняется НА КАЖДОМ старте и гарантирует:
  - схему + таблицу (CREATE ... IF NOT EXISTS),
  - все колонки (ADD COLUMN IF NOT EXISTS — nullable safety-net для частичной таблицы),
  - UNIQUE-таргет под ON CONFLICT (CREATE UNIQUE INDEX IF NOT EXISTS на тройку),
  - вспомогательный индекс (account_id, scope).
Дополнительно логирует, существовал ли UNIQUE-таргет ДО запуска — ground truth о том,
была ли таблица сломана.

Схема выведена из всех обращений db/queries/consent.py:
  INSERT (account_id, scope, granted, consent_version, granted_at, interface)
    ON CONFLICT (account_id, scope, consent_version) → нужен UNIQUE на эту тройку
  UPDATE granted/revoked_at WHERE account_id AND scope
  SELECT granted / SELECT id ... ORDER BY granted_at DESC

Запуск вручную:
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

# Safety-net на случай, если таблица была создана раньше с неполным набором колонок.
# Только nullable ADD COLUMN — NOT NULL на таблице с данными упал бы.
ADD_COLUMNS_SQL = [
    "ALTER TABLE learning.consent_grant ADD COLUMN IF NOT EXISTS account_id UUID",
    "ALTER TABLE learning.consent_grant ADD COLUMN IF NOT EXISTS scope TEXT",
    "ALTER TABLE learning.consent_grant ADD COLUMN IF NOT EXISTS granted BOOLEAN",
    "ALTER TABLE learning.consent_grant ADD COLUMN IF NOT EXISTS consent_version TEXT DEFAULT 'v1.0'",
    "ALTER TABLE learning.consent_grant ADD COLUMN IF NOT EXISTS granted_at TIMESTAMPTZ DEFAULT NOW()",
    "ALTER TABLE learning.consent_grant ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ",
    "ALTER TABLE learning.consent_grant ADD COLUMN IF NOT EXISTS interface TEXT",
]

# Гарантирует таргет под ON CONFLICT (account_id, scope, consent_version).
# UNIQUE INDEX (а не named constraint) — потому что ON CONFLICT инференс работает и
# по unique-индексу, а IF NOT EXISTS даёт идемпотентность без знания имени старого constraint.
UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_consent_grant_acct_scope_ver
ON learning.consent_grant (account_id, scope, consent_version);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_consent_grant_account_scope
ON learning.consent_grant (account_id, scope);
"""

HAD_UNIQUE_TARGET_SQL = """
SELECT
  EXISTS(
    SELECT 1 FROM pg_catalog.pg_constraint
    WHERE conrelid = to_regclass('learning.consent_grant') AND contype = 'u'
  )
  OR EXISTS(
    SELECT 1 FROM pg_catalog.pg_indexes
    WHERE schemaname = 'learning' AND tablename = 'consent_grant'
      AND indexdef ILIKE '%UNIQUE%'
      AND indexdef ILIKE '%account_id%'
      AND indexdef ILIKE '%scope%'
      AND indexdef ILIKE '%consent_version%'
  )
"""


async def _ensure_schema(conn) -> dict:
    """Идемпотентно гарантирует схему consent_grant. Возвращает диагностику."""
    table_existed = await conn.fetchval("SELECT to_regclass('learning.consent_grant')") is not None
    had_unique = False
    if table_existed:
        had_unique = bool(await conn.fetchval(HAD_UNIQUE_TARGET_SQL))

    await conn.execute("CREATE SCHEMA IF NOT EXISTS learning")
    await conn.execute(TABLE_SQL)
    for stmt in ADD_COLUMNS_SQL:
        await conn.execute(stmt)
    await conn.execute(UNIQUE_INDEX_SQL)
    await conn.execute(INDEX_SQL)
    return {"table_existed": table_existed, "had_unique_target": had_unique}


async def migrate():
    conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)
    try:
        async with conn.transaction():
            diag = await _ensure_schema(conn)
        print(f"  consent_grant ensure-schema OK; table_existed={diag['table_existed']} "
              f"had_unique_target_before={diag['had_unique_target']}")
        print("Миграция 023 завершена")
        return diag
    finally:
        await conn.close()


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    """Всегда выполняет идемпотентную ensure-схему (фиксит и предсуществующую
    таблицу без UNIQUE-таргета). Возвращает True только если таблица создана впервые."""
    diag = await migrate()
    return not diag["table_existed"]


if __name__ == "__main__":
    asyncio.run(migrate())
