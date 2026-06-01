"""
Dry-run: сравнение marathon_progress.total_checkins (колонка)
с COUNT(DISTINCT day) FROM marathon_state (derived).

WP-330 P1: перед деплоем ca52eb4/b64ff16 проверяем, есть ли
backfill-расхождения у активных пользователей.

Запуск:
    python -m scripts.wp330_dry_run_total_checkins

Выход:
    - Список user_id с расхождением
    - Итоговая статистика
    - Код возврата 1 если есть расхождения, 0 если всё совпадает
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import LEARNING_URL


DRY_RUN_SQL = """
SELECT mp.user_id,
       mp.current_day,
       mp.total_checkins AS column_value,
       COUNT(DISTINCT ms.day) AS derived_value,
       mp.total_checkins - COUNT(DISTINCT ms.day) AS diff
FROM learning.marathon_progress mp
LEFT JOIN learning.marathon_state ms ON ms.user_id = mp.user_id
WHERE mp.status = 'active'
GROUP BY mp.user_id
HAVING mp.total_checkins != COUNT(DISTINCT ms.day)
   OR (mp.total_checkins IS NULL AND COUNT(DISTINCT ms.day) > 0)
ORDER BY ABS(mp.total_checkins - COUNT(DISTINCT ms.day)) DESC
"""


async def dry_run():
    conn = await asyncpg.connect(LEARNING_URL)
    try:
        rows = await conn.fetch(DRY_RUN_SQL)
        if not rows:
            print("✅ Расхождений не найдено. Колонка total_checkins совпадает с derived count для всех активных пользователей.")
            return 0

        print(f"⚠️  Найдено {len(rows)} активных пользователей с расхождением:\n")
        print(f"{'user_id':>12} | {'current_day':>11} | {'column':>8} | {'derived':>8} | {'diff':>6}")
        print("-" * 60)
        for r in rows:
            print(f"{r['user_id']:>12} | {r['current_day']:>11} | {r['column_value'] or 0:>8} | {r['derived_value']:>8} | {r['diff'] or 0:>6}")

        print(f"\n📊 Итог: {len(rows)} пользователей нуждаются в коррекции.")
        print("   Рекомендация: запустить миграцию или ручной UPDATE после проверки.")
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    code = asyncio.run(dry_run())
    sys.exit(code)
