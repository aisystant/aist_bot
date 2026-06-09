"""
Проверка расхождений marathon_progress.total_checkins (колонка) vs
COUNT(DISTINCT day) FROM marathon_state (derived).

WP-330 P1 followup: диагностика до/после migration 026.
После запуска migration 026 расхождений быть не должно — триггер
auto-sync держит колонку актуальной при каждом INSERT/UPDATE/DELETE
в marathon_state.

Запуск:
    python -m scripts.wp330_dry_run_total_checkins          # только диагностика
    python -m scripts.wp330_dry_run_total_checkins --apply  # backfill без триггера

Выход:
    - Список всех пользователей с расхождением (все статусы)
    - Итоговая статистика (затронутые / всего активных)
    - Код возврата 1 если есть расхождения, 0 если всё совпадает
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import LEARNING_URL


# Проверяем все статусы — у completed/dropped пользователей данные тоже могут расходиться.
CHECK_SQL = """
SELECT mp.user_id,
       mp.status,
       mp.current_day,
       mp.total_checkins                AS column_value,
       COUNT(DISTINCT ms.day)           AS derived_value,
       mp.total_checkins - COUNT(DISTINCT ms.day) AS diff
FROM learning.marathon_progress mp
LEFT JOIN learning.marathon_state ms ON ms.user_id = mp.user_id
GROUP BY mp.user_id, mp.status, mp.current_day, mp.total_checkins
HAVING mp.total_checkins IS DISTINCT FROM COUNT(DISTINCT ms.day)
ORDER BY ABS(mp.total_checkins - COUNT(DISTINCT ms.day)) DESC, mp.status
"""

ACTIVE_TOTAL_SQL = """
SELECT COUNT(*) FROM learning.marathon_progress WHERE status = 'active'
"""

# Используется только при --apply (без триггера).
APPLY_SQL = """
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


async def run(apply: bool = False) -> int:
    conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)
    try:
        rows = await conn.fetch(CHECK_SQL)
        active_total = await conn.fetchval(ACTIVE_TOTAL_SQL)

        if not rows:
            print("✅ Расхождений нет. total_checkins совпадает с derived count у всех пользователей.")
            return 0

        active_mismatch = sum(1 for r in rows if r["status"] == "active")
        zero_count = sum(1 for r in rows if (r["column_value"] or 0) == 0 and r["derived_value"] > 0)

        print(f"⚠️  Найдено {len(rows)} пользователей с расхождением "
              f"(активных: {active_mismatch}/{active_total}, total_checkins=0: {zero_count}):\n")
        print(f"{'user_id':>12} | {'status':>10} | {'day':>4} | {'column':>7} | {'derived':>8} | {'diff':>6}")
        print("-" * 65)
        for r in rows:
            col = r["column_value"] if r["column_value"] is not None else "NULL"
            diff = (r["column_value"] or 0) - r["derived_value"]
            print(f"{r['user_id']:>12} | {r['status']:>10} | {r['current_day']:>4} | "
                  f"{col!s:>7} | {r['derived_value']:>8} | {diff:>6}")

        print(f"\n📊 Итог: {len(rows)} пользователей нуждаются в коррекции.")

        if apply:
            result = await conn.execute(APPLY_SQL)
            updated = int(result.split()[-1])
            print(f"\n✅ Применено: обновлено {updated} строк.")
            print("Триггер не создан — запустите migration 026 для защиты от повторения:")
            print("    python -m db.migrations.026_sync_total_checkins_trigger")
        else:
            print("\nДля исправления:")
            print("  python -m db.migrations.026_sync_total_checkins_trigger  (backfill + триггер)")

        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    code = asyncio.run(run(apply=apply_mode))
    sys.exit(code)
