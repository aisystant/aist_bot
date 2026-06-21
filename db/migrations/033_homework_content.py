"""
Migration 033: learning.homework_content — LMS homework answer texts.

# see WP-427 Ф6.1 (consent-gated LMS text capture)

Table stores PII-stripped homework answers for users who gave text_analysis consent.
Writer: bridge-2-lms-homework poller (via event-gateway → multi-domain-projection-worker).
UPSERT key: (ory_id, external_id) — one row per LMS taskanswer per user.

Requires LEARNING_DATABASE_URL (learning DB, not the bot's primary DB).

Projection rule SQL (run against reference DB separately):
  INSERT INTO projection_rules (
    event_type, target_db, target_schema, target_table,
    upsert_key, columns, field_map
  )
  VALUES (
    'lms_homework_content',
    'learning', 'public', 'homework_content',
    ARRAY['ory_id', 'external_id'],
    ARRAY['ory_id', 'external_id', 'task_id', 'status', 'answer_text', 'task_text', 'ingested_at'],
    '{
      "_where": {
        "ory_id":      "$.account_id",
        "external_id": "$.payload.external_id"
      },
      "_set": {
        "task_id":     "$.payload.task_id",
        "status":      "$.payload.status",
        "answer_text": "$.payload.answer_text",
        "task_text":   "$.payload.task_text",
        "ingested_at": "$.ingested_at"
      }
    }'::jsonb
  )
  ON CONFLICT (event_type) DO NOTHING;

Manual run:
    LEARNING_DATABASE_URL=<dsn> python -m db.migrations.033_homework_content
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
                  AND table_name = 'homework_content'
            )
            """
        )
        if exists:
            return False

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS public.homework_content (
                id          BIGSERIAL PRIMARY KEY,
                ory_id      UUID NOT NULL,
                external_id BIGINT NOT NULL,
                task_id     BIGINT,
                status      INTEGER,
                answer_text TEXT NOT NULL,
                task_text   TEXT,
                ingested_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (ory_id, external_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_homework_content_ory_id
            ON public.homework_content (ory_id)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_homework_content_task_id
            ON public.homework_content (task_id)
            WHERE task_id IS NOT NULL
            """
        )
    return True


if __name__ == "__main__":
    dsn = os.environ.get("LEARNING_DATABASE_URL")
    if not dsn:
        print("Error: LEARNING_DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    async def run():
        pool = await asyncpg.create_pool(dsn)
        created = await migrate_if_needed(pool)
        print(f"Migration 033: {'homework_content created' if created else 'already exists'}")
        await pool.close()

    asyncio.run(run())
