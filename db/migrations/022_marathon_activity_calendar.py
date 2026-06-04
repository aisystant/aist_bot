"""
Миграция 022: факт-таблица календарной активности marathon_activity + backfill WP-330.

Peer-session 2026-06-03-16-lms-attendance-bot-port (Kimi + Claude):
- День = календарная дата 00:00–23:59 MSK (как в LMS PassingAction).
- Факт-таблица: learning.marathon_activity(user_id, activity_date, action_type, raw_count).
- Не храним missed_streak — считаем через CTE on-demand.
- Nightly batch агрегирует marathon_state за предыдущий день и upsert'ит сюда.
- Backfill: заполняем исторические check-ins из marathon_state ретроспективно.

Запуск:
    python -m db.migrations.022_marathon_activity_calendar
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import LEARNING_URL


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS learning.marathon_activity (
    user_id         BIGINT      NOT NULL,
    activity_date   DATE        NOT NULL,
    action_type     TEXT        NOT NULL DEFAULT 'checkin',
    raw_count       INTEGER     NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, activity_date, action_type)
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_marathon_activity_user_date
ON learning.marathon_activity(user_id, activity_date);
"""

BACKFILL_SQL = """
INSERT INTO learning.marathon_activity (user_id, activity_date, action_type, raw_count)
SELECT
    user_id,
    (check_in_at AT TIME ZONE 'Europe/Moscow')::DATE AS activity_date,
    'checkin'::TEXT AS action_type,
    COUNT(*)::INTEGER AS raw_count
FROM learning.marathon_state
WHERE check_in_at IS NOT NULL
GROUP BY user_id, (check_in_at AT TIME ZONE 'Europe/Moscow')::DATE
ON CONFLICT (user_id, activity_date, action_type)
DO UPDATE SET
    raw_count = EXCLUDED.raw_count,
    updated_at = NOW()
"""

VIEW_SQL = """
CREATE VIEW learning.marathon_stats AS
SELECT
    mp.user_id,
    mp.current_day,
    mp.status,
    mp.started_at,
    mp.completed_at,
    COUNT(DISTINCT ms.day) AS total_checkins,
    GREATEST(mp.current_day - COUNT(DISTINCT ms.day), 0) AS missed_days,
    mp.badge_list,
    count(ms.day) FILTER (WHERE ms.state = 'chaos'::text) AS chaos_days,
    count(ms.day) FILTER (WHERE ms.state = 'stuck'::text) AS stuck_days,
    count(ms.day) FILTER (WHERE ms.state = 'turn'::text) AS turn_days,
    max(ms.check_in_at) AS last_check_in_at,
    mp.updated_at
FROM learning.marathon_progress mp
LEFT JOIN learning.marathon_state ms ON ms.user_id = mp.user_id
GROUP BY mp.user_id
"""


async def migrate():
    conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)
    try:
        async with conn.transaction():
            # 1. Create table
            await conn.execute(TABLE_SQL)
            print("  CREATE TABLE learning.marathon_activity — OK")

            # 2. Create index
            await conn.execute(INDEX_SQL)
            print("  CREATE INDEX idx_marathon_activity_user_date — OK")

            # 3. Backfill from marathon_state (WP-330 historical check-ins)
            result = await conn.execute(BACKFILL_SQL)
            # result is a command tag like "INSERT 0 123"
            print(f"  Backfill marathon_activity from marathon_state — {result}")

            # 4. Recreate marathon_stats view unchanged (keep compatibility)
            await conn.execute("DROP VIEW IF EXISTS learning.marathon_stats")
            print("  DROP VIEW learning.marathon_stats — OK")
            await conn.execute(VIEW_SQL)
            print("  CREATE VIEW learning.marathon_stats — OK")

        print("Миграция 022 завершена")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
