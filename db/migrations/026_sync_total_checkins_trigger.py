"""
Миграция 026: backfill total_checkins + триггер auto-sync.

Проблема (WP-330 followup, 2026-06-09):
  39 из 77 активных пользователей: total_checkins != COUNT(DISTINCT day).
  У 38 из них total_checkins = 0 при наличии реальных чек-инов (разброс -1..-7).
  Причина: migration 021 переключила VIEW marathon_stats на derived-расчёт,
  но физическая колонка marathon_progress.total_checkins перестала обновляться —
  старый код её инкрементировал, новый код (WP-330 P1) — нет.
  Metabase-дашборды и MCP, читающие колонку напрямую, видят stale-нули.

Решение:
  1. Backfill: обновить колонку для всех пользователей (все статусы), где
     есть расхождение с derived count.
  2. Trigger: при любом INSERT/UPDATE/DELETE в marathon_state автоматически
     пересчитывать total_checkins для затронутого user_id.
     Источник истины остаётся marathon_state (WP-330 P1, peer-session 2026-06-01).

Запуск:
    python -m db.migrations.026_sync_total_checkins_trigger
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import LEARNING_URL


# Обновляем только строки с реальным расхождением (IS DISTINCT FROM учитывает NULL).
BACKFILL_SQL = """
UPDATE learning.marathon_progress mp
SET    total_checkins = subq.cnt,
       updated_at     = NOW()
FROM (
    SELECT mp2.user_id,
           COUNT(DISTINCT ms.day) AS cnt
    FROM   learning.marathon_progress mp2
    LEFT JOIN learning.marathon_state ms ON ms.user_id = mp2.user_id
    GROUP BY mp2.user_id
) subq
WHERE mp.user_id = subq.user_id
  AND mp.total_checkins IS DISTINCT FROM subq.cnt
"""

# CREATE OR REPLACE — идемпотентна, безопасно перезапускать миграцию.
TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION learning.sync_total_checkins()
RETURNS TRIGGER AS $$
DECLARE
    v_user_id BIGINT;
BEGIN
    -- DELETE: NEW is NULL, нужен OLD.user_id
    -- INSERT/UPDATE: NEW.user_id
    v_user_id := COALESCE(NEW.user_id, OLD.user_id);

    UPDATE learning.marathon_progress
    SET    total_checkins = (
               SELECT COUNT(DISTINCT day)
               FROM   learning.marathon_state
               WHERE  user_id = v_user_id
           ),
           updated_at = NOW()
    WHERE  user_id = v_user_id;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
"""

DROP_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS marathon_state_sync_total_checkins
ON learning.marathon_state
"""

CREATE_TRIGGER_SQL = """
CREATE TRIGGER marathon_state_sync_total_checkins
AFTER INSERT OR UPDATE OR DELETE ON learning.marathon_state
FOR EACH ROW EXECUTE FUNCTION learning.sync_total_checkins()
"""

VERIFY_SQL = """
SELECT COUNT(*) AS mismatch_count
FROM (
    SELECT mp.user_id
    FROM   learning.marathon_progress mp
    LEFT JOIN learning.marathon_state ms ON ms.user_id = mp.user_id
    GROUP BY mp.user_id, mp.total_checkins
    HAVING mp.total_checkins IS DISTINCT FROM COUNT(DISTINCT ms.day)
) sub
"""


async def migrate() -> None:
    conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)
    try:
        async with conn.transaction():
            result = await conn.execute(BACKFILL_SQL)
            # asyncpg возвращает строку вида "UPDATE N"
            updated = int(result.split()[-1])
            print(f"  Backfill: обновлено {updated} пользователей — OK")

            await conn.execute(TRIGGER_FUNCTION_SQL)
            print("  CREATE FUNCTION learning.sync_total_checkins() — OK")

            await conn.execute(DROP_TRIGGER_SQL)
            await conn.execute(CREATE_TRIGGER_SQL)
            print("  CREATE TRIGGER marathon_state_sync_total_checkins — OK")

        # Верификация вне транзакции — смотрим реальное состояние после коммита.
        row = await conn.fetchrow(VERIFY_SQL)
        mismatches = row["mismatch_count"] if row else 0
        if mismatches == 0:
            print("  Верификация: расхождений нет — OK")
        else:
            print(f"  ⚠️  Верификация: {mismatches} расхождений остались — требует ручной проверки")
            print("  Запустить: python -m scripts.wp330_dry_run_total_checkins")

        print("Миграция 026 завершена")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
