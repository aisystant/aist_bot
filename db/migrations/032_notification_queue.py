"""
Миграция 032: таблица очереди доставки сообщений (notification_queue).

# see DP.SC.135, DP.ROLE.045
# (WP-418 Ф3, peer-session 2026-06-12-06-wp418-delivery-f4-migration)

notification_queue — единая точка исходящих сообщений бота (Доставщик).
Таблица определена в db/models.py (create_tables), но в проде create_tables
пропускается флагом SKIP_DB_MIGRATIONS — нужен явный идемпотентный файл.

Колонки:
  id                — SERIAL PRIMARY KEY
  chat_id           — получатель
  notification_class — класс (capped/critical/must-deliver/transactional/ops-alert)
  payload           — JSONB с text/format/…
  priority          — порядок в дренаже (0–9)
  dedup_key         — уникальность входа (TEXT, nullable)
  journal_key       — ключ записи в notification_log (контракт was_nudge_sent_recently)
  journal_type      — тип записи в notification_log
  status            — queued → sent/suppressed/failed
  reason            — причина suppressed/failed (nullable)
  scheduled_at      — не раньше этого времени
  locked_at         — дренаж держит lock (nullable)
  attempts          — сколько раз пробовали
  created_at        — время постановки в очередь
  sent_at           — время доставки (nullable)

Запуск вручную:
    python -m db.migrations.032_notification_queue
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'notification_queue'
            )
            """
        )
        if exists:
            # Страховка: добавить journal_* если таблица создана без них
            await conn.execute(
                """
                ALTER TABLE notification_queue
                ADD COLUMN IF NOT EXISTS journal_key TEXT,
                ADD COLUMN IF NOT EXISTS journal_type TEXT
                """
            )
            return False

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_queue (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                notification_class TEXT NOT NULL,
                payload JSONB NOT NULL,
                priority INTEGER NOT NULL,
                dedup_key TEXT,
                journal_key TEXT,
                journal_type TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                reason TEXT,
                scheduled_at TIMESTAMP DEFAULT NOW(),
                locked_at TIMESTAMP,
                attempts INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                sent_at TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_notif_queue_cap
            ON notification_queue(chat_id, notification_class, created_at)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_notif_queue_drain
            ON notification_queue(status, priority, scheduled_at)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_notif_queue_dedup
            ON notification_queue(dedup_key)
            """
        )
    return True


if __name__ == "__main__":
    from config import DATABASE_URL

    async def run():
        pool = await asyncpg.create_pool(DATABASE_URL)
        created = await migrate_if_needed(pool)
        print(f"Migration 032: {'notification_queue created' if created else 'already exists (journal columns ensured)'}")
        await pool.close()

    asyncio.run(run())
