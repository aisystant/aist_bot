"""
Миграция 020: удаление мёртвой колонки missed_days из learning.marathon_progress.

После WP-330 fix (peer-session 2026-05-29-16-marathon-bot-checkin-terminology):
- /marathon_progress, weekly digest, mentor alert, nudge — все используют формулу
  max(0, current_day - total_checkins). Колонка missed_days больше не читается из кода.
- handlers/marathon.py:69 (start_marathon_flow) и db/queries/marathon_newcomer.py:107
  (update_progress параметр) очищены отдельно.

Однако: learning.marathon_stats view (аналитика для Metabase / dt-collect)
использует missed_days в SELECT-листе. Чтобы не сломать внешних потребителей,
пересоздаём view с формулой GREATEST(current_day - total_checkins, 0) AS missed_days —
семантика остаётся та же (computed at query time), потребители не замечают.

Последовательность:
  1. DROP VIEW learning.marathon_stats
  2. ALTER TABLE learning.marathon_progress DROP COLUMN missed_days
  3. CREATE VIEW learning.marathon_stats с формулой

CLAUDE.md §10.22: CREATE OR REPLACE VIEW запрещён → DROP + CREATE.

Запуск:
    python -m db.migrations.020_drop_marathon_progress_missed_days
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import LEARNING_URL


VIEW_SQL = """
CREATE VIEW learning.marathon_stats AS
SELECT mp.user_id,
       mp.current_day,
       mp.status,
       mp.started_at,
       mp.completed_at,
       mp.total_checkins,
       GREATEST(mp.current_day - mp.total_checkins, 0) AS missed_days,
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
    conn = await asyncpg.connect(LEARNING_URL)
    try:
        col = await conn.fetchval(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'learning'
              AND table_name   = 'marathon_progress'
              AND column_name  = 'missed_days'
            """
        )
        if not col:
            print("learning.marathon_progress.missed_days: уже отсутствует — пропускаю")
            return

        async with conn.transaction():
            await conn.execute("DROP VIEW IF EXISTS learning.marathon_stats")
            print("  DROP VIEW learning.marathon_stats — OK")

            await conn.execute(
                "ALTER TABLE learning.marathon_progress DROP COLUMN missed_days"
            )
            print("  ALTER TABLE ... DROP COLUMN missed_days — OK")

            await conn.execute(VIEW_SQL)
            print("  CREATE VIEW learning.marathon_stats (с формулой missed_days) — OK")

        print("Миграция 020 завершена")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
