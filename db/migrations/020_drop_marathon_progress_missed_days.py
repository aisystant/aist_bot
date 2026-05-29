"""
Миграция 020: удаление мёртвой колонки missed_days из learning.marathon_progress.

После WP-330 fix (peer-session 2026-05-29-16-marathon-bot-checkin-terminology):
- /marathon_progress, weekly digest, mentor alert, nudge — все используют формулу
  max(0, current_day - total_checkins). Колонка missed_days больше не читается.
- handlers/marathon.py:69 (start_marathon_flow) и db/queries/marathon_newcomer.py:107
  (update_progress параметр) очищены отдельно.

После этой миграции колонка удалена окончательно.

Запуск:
    python -m db.migrations.020_drop_marathon_progress_missed_days
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import LEARNING_URL


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

        await conn.execute(
            "ALTER TABLE learning.marathon_progress DROP COLUMN missed_days"
        )
        print("learning.marathon_progress.missed_days: удалена")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
