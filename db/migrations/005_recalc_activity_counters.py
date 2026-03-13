"""
Миграция 005: Пересчёт active_days_total, active_days_streak, longest_streak.

Причина: touch_last_active_date в TracingMiddleware ставил last_active_date
без инкремента счётчиков → record_active_day пропускал обновление.
Source-of-truth: activity_log.

Запуск:
    python -m db.migrations.005_recalc_activity_counters
"""

import asyncio
import asyncpg
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Пересчитать счётчики активности из activity_log."""
    print("Подключение к базе данных...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        # Получаем всех пользователей с activity_log
        rows = await conn.fetch('''
            SELECT chat_id, array_agg(DISTINCT activity_date ORDER BY activity_date) as dates
            FROM activity_log
            GROUP BY chat_id
        ''')

        print(f"Найдено пользователей с активностью: {len(rows)}")

        # Определяем «сегодня» по МСК (UTC+3)
        today_row = await conn.fetchval("SELECT (NOW() AT TIME ZONE 'Europe/Moscow')::date")
        print(f"Сегодня (МСК): {today_row}")

        updated = 0
        for row in rows:
            chat_id = row['chat_id']
            dates = sorted(row['dates'])

            if not dates:
                continue

            total = len(dates)
            last_active = dates[-1]

            # Считаем все streak'и (gaps-and-islands)
            longest = 1
            current_len = 1

            for i in range(1, len(dates)):
                if dates[i] - dates[i - 1] == timedelta(days=1):
                    current_len += 1
                else:
                    longest = max(longest, current_len)
                    current_len = 1
            longest = max(longest, current_len)

            # Текущая серия: streak, заканчивающийся сегодня или вчера
            streak = 0
            if last_active >= today_row - timedelta(days=1):
                streak = 1
                for i in range(len(dates) - 2, -1, -1):
                    if dates[i + 1] - dates[i] == timedelta(days=1):
                        streak += 1
                    else:
                        break

            await conn.execute('''
                UPDATE development.user_state
                SET active_days_total = $2,
                    active_days_streak = $3,
                    longest_streak = $4,
                    last_active_date = $5
                WHERE chat_id = $1
            ''', chat_id, total, streak, longest, last_active)
            updated += 1

        print(f"Обновлено пользователей: {updated}")

        # Проверка: показать итоговые значения
        check = await conn.fetch('''
            SELECT chat_id, active_days_total, active_days_streak, longest_streak, last_active_date
            FROM development.user_state
            WHERE active_days_total > 0
            ORDER BY active_days_total DESC
            LIMIT 10
        ''')
        print("\nТоп-10 по активности после пересчёта:")
        for r in check:
            print(f"  {r['chat_id']}: total={r['active_days_total']}, "
                  f"streak={r['active_days_streak']}, longest={r['longest_streak']}, "
                  f"last={r['last_active_date']}")

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(migrate())
