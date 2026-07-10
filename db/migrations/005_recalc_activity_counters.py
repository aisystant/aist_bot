"""
Миграция 005: Пересчёт active_days_total, active_days_streak, longest_streak.

Причина: touch_last_active_date в TracingMiddleware ставил last_active_date
без инкремента счётчиков → record_active_day пропускал обновление.
Source-of-truth: activity_log (learning DB, WP-268 Phase 5 G5).

activity_log — learning DB (LEARNING_URL).
development.user_state — dev DB (DATABASE_URL).
Нельзя читать cross-database — используем два отдельных соединения.

Idempotency: флаг `activity_counters_recalculated_at` в development.user_state.
Пересчёт пропускает пользователей, у которых флаг уже установлен.

Запуск вручную:
    python -m db.migrations.005_recalc_activity_counters

Запуск из бота (bot.py startup):
    await migrate_if_needed(dev_pool, learning_pool)
"""

import asyncio
import asyncpg
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL, LEARNING_URL


async def _ensure_flag_column(dev_conn: asyncpg.Connection) -> None:
    """Добавить колонку-флаг idempotency, если её нет."""
    await dev_conn.execute("""
        ALTER TABLE development.user_state
        ADD COLUMN IF NOT EXISTS activity_counters_recalculated_at TIMESTAMP DEFAULT NULL
    """)


async def _recalc(dev_conn: asyncpg.Connection, learning_conn: asyncpg.Connection) -> int:
    """Пересчитать счётчики для пользователей без флага. Вернуть кол-во обновлённых."""
    rows = await learning_conn.fetch('''
        SELECT chat_id, array_agg(DISTINCT activity_date ORDER BY activity_date) as dates
        FROM activity_log
        GROUP BY chat_id
    ''')

    today_row = await learning_conn.fetchval(
        "SELECT (NOW() AT TIME ZONE 'Europe/Moscow')::date"
    )

    # Кто уже пересчитан — пропускаем
    done_ids = set(await dev_conn.fetch(
        "SELECT chat_id FROM development.user_state WHERE activity_counters_recalculated_at IS NOT NULL"
    ))
    done_chat_ids = {r['chat_id'] for r in done_ids}

    updated = 0
    for row in rows:
        chat_id = row['chat_id']
        if chat_id in done_chat_ids:
            continue

        dates = sorted(row['dates'])
        if not dates:
            continue

        total = len(dates)
        last_active = dates[-1]

        longest = 1
        current_len = 1
        for i in range(1, len(dates)):
            if dates[i] - dates[i - 1] == timedelta(days=1):
                current_len += 1
            else:
                longest = max(longest, current_len)
                current_len = 1
        longest = max(longest, current_len)

        streak = 0
        if last_active >= today_row - timedelta(days=1):
            streak = 1
            for i in range(len(dates) - 2, -1, -1):
                if dates[i + 1] - dates[i] == timedelta(days=1):
                    streak += 1
                else:
                    break

        await dev_conn.execute('''
            UPDATE development.user_state
            SET active_days_total = $2,
                active_days_streak = $3,
                longest_streak = $4,
                last_active_date = $5,
                activity_counters_recalculated_at = NOW()
            WHERE chat_id = $1
        ''', chat_id, total, streak, longest, last_active)
        updated += 1

    return updated


async def migrate_if_needed(dev_pool: asyncpg.Pool, learning_pool: asyncpg.Pool) -> bool:
    """Пересчитать счётчики для пользователей без флага. Вернуть True если запустилась."""
    async with dev_pool.acquire() as dev_conn:
        await _ensure_flag_column(dev_conn)
        pending = await dev_conn.fetchval(
            "SELECT COUNT(*) FROM development.user_state WHERE activity_counters_recalculated_at IS NULL"
        )
        if pending == 0:
            return False

    async with dev_pool.acquire() as dev_conn, learning_pool.acquire() as learning_conn:
        updated = await _recalc(dev_conn, learning_conn)

    return updated > 0


async def migrate():
    """Пересчитать счётчики активности из activity_log (ручной запуск)."""
    print("Подключение к learning-БД (activity_log)...")
    learning_conn = await asyncpg.connect(LEARNING_URL)
    print("Подключение к dev-БД (development.user_state)...")
    dev_conn = await asyncpg.connect(DATABASE_URL)

    try:
        await _ensure_flag_column(dev_conn)

        updated = await _recalc(dev_conn, learning_conn)
        print(f"Обновлено пользователей: {updated}")

        check = await dev_conn.fetch('''
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
        await learning_conn.close()
        await dev_conn.close()


if __name__ == '__main__':
    asyncio.run(migrate())
