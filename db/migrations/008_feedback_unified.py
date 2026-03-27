"""
Миграция 008: Расширение feedback_triage → feedback_unified (WP-178).

Добавляет поля lifecycle конвейера обратной связи:
  - source: откуда пришла жалоба (bot, club, manual)
  - status: lifecycle тикета (new → classified → routed → in_progress → resolved → confirmed)
  - suggested_action: рекомендация Триажёра (add_faq, fix_bug, add_feature, etc.)
  - resolved_at: когда закрыт
  - resolution_note: объяснение (обязательно для wontfix)
  - confidence: уверенность классификатора (0.0-1.0)

НЕ переименовывает таблицу (обратная совместимость).
Создаёт VIEW feedback_unified как alias.

Связь: PROCESSES.md §6, SC-19 (WP-74), АрхГейт 58/70.

Запуск:
    python -m db.migrations.008_feedback_unified
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Добавить lifecycle-поля в feedback_triage."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Проверяем, не применена ли уже миграция
        has_status = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'feedback_triage' AND column_name = 'source'
        """)
        if has_status > 0:
            print("Migration 008 already applied, skipping.")
            return

        print("Applying migration 008: feedback_unified lifecycle fields...")

        await conn.execute("""
            -- Lifecycle поля
            ALTER TABLE feedback_triage
                ADD COLUMN source TEXT NOT NULL DEFAULT 'bot',
                ADD COLUMN suggested_action TEXT DEFAULT NULL,
                ADD COLUMN confidence REAL DEFAULT NULL,
                ADD COLUMN resolved_at TIMESTAMP DEFAULT NULL,
                ADD COLUMN resolution_note TEXT DEFAULT NULL;
        """)

        # status уже существует (default 'new'), но нужно расширить допустимые значения
        # Проверяем, есть ли status
        has_existing_status = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'feedback_triage' AND column_name = 'status'
        """)
        if has_existing_status == 0:
            await conn.execute("""
                ALTER TABLE feedback_triage
                    ADD COLUMN status TEXT NOT NULL DEFAULT 'new';
            """)

        # Обновляем существующие тикеты: status='new' → 'classified' (они уже классифицированы)
        updated = await conn.execute("""
            UPDATE feedback_triage
            SET status = 'classified'
            WHERE status = 'new'
              AND category IS NOT NULL
              AND category != 'unknown';
        """)
        print(f"  Updated existing classified tickets: {updated}")

        # Индексы для lifecycle-запросов
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_triage_status
                ON feedback_triage (status);
            CREATE INDEX IF NOT EXISTS idx_feedback_triage_source
                ON feedback_triage (source);
            CREATE INDEX IF NOT EXISTS idx_feedback_triage_resolved
                ON feedback_triage (resolved_at)
                WHERE resolved_at IS NOT NULL;
        """)

        # View как alias (для будущего кода, который будет использовать feedback_unified)
        await conn.execute("""
            CREATE OR REPLACE VIEW feedback_unified AS
            SELECT * FROM feedback_triage;
        """)

        print("Migration 008 applied successfully.")
        print("  Added: source, suggested_action, confidence, resolved_at, resolution_note")
        print("  Indexes: status, source, resolved_at")
        print("  View: feedback_unified → feedback_triage")

    finally:
        await conn.close()


async def rollback():
    """Откат миграции 008."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        print("Rolling back migration 008...")
        await conn.execute("DROP VIEW IF EXISTS feedback_unified;")
        await conn.execute("DROP INDEX IF EXISTS idx_feedback_triage_status;")
        await conn.execute("DROP INDEX IF EXISTS idx_feedback_triage_source;")
        await conn.execute("DROP INDEX IF EXISTS idx_feedback_triage_resolved;")
        await conn.execute("""
            ALTER TABLE feedback_triage
                DROP COLUMN IF EXISTS source,
                DROP COLUMN IF EXISTS suggested_action,
                DROP COLUMN IF EXISTS confidence,
                DROP COLUMN IF EXISTS resolved_at,
                DROP COLUMN IF EXISTS resolution_note;
        """)
        print("Rollback 008 done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--rollback":
        asyncio.run(rollback())
    else:
        asyncio.run(migrate())
