"""
Миграция 021: пересоздание learning.marathon_stats с derived total_checkins.

WP-330 P1 fix (peer-session 2026-06-01):
- total_checkins больше не инкрементируется в коде, источник истины — marathon_state.
- View marathon_stats обновлён: total_checkins = COUNT(DISTINCT ms.day),
  missed_days = GREATEST(current_day - COUNT(DISTINCT ms.day), 0).

CLAUDE.md §10.22: CREATE OR REPLACE VIEW запрещён → DROP + CREATE.

Запуск:
    python -m db.migrations.021_marathon_stats_derived_checkins
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
    conn = await asyncpg.connect(LEARNING_URL)
    try:
        async with conn.transaction():
            await conn.execute("DROP VIEW IF EXISTS learning.marathon_stats")
            print("  DROP VIEW learning.marathon_stats — OK")

            await conn.execute(VIEW_SQL)
            print("  CREATE VIEW learning.marathon_stats (derived total_checkins) — OK")

        print("Миграция 021 завершена")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
