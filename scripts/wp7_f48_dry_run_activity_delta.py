"""
Dry-run: сравнение development.user_state.active_days_total (счётчик) с
COUNT(DISTINCT activity_date) FROM activity_log (журнал) — WP-7 Ф48,
peer-session 2026-08-04-27-wp7-f48-bot-reliability.

НЕ выполняет backfill. Отчёт только читает обе БД и печатает распределение
дельт — решение о реальном backfill (и его метод) принимает пилот после
просмотра цифр (Codex: «нужен dry-run, а не немедленный UPDATE», консенсус
turn 3).

Дельта не доказывает только гонку touch/record (исправлена этой же сессией):
- log > counter: гонка touch, сбой INSERT в activity_log, либо старые записи после reset
- counter > log: сбои INSERT, исторический период до появления журнала, либо старый путь записи
- совпадение не исключает взаимно компенсирующих ошибок

Пользователи с активностью РОВНО в день stats_reset_date помечены отдельно —
дата хранится с точностью до дня, поэтому нельзя отличить активность до
сброса от активности после сброса в тот же день (Codex, turn 3).

Запуск:
    python -m scripts.wp7_f48_dry_run_activity_delta

Выход:
    - Таблица дельт по бакетам (log>counter / counter>log / match), топ-20 по модулю дельты
    - Отдельный список неоднозначных (активность в день stats_reset_date)
    - Код возврата всегда 0 — это отчёт, не проверка на pass/fail
"""

import asyncio
import asyncpg
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATABASE_URL, LEARNING_URL


async def dry_run():
    dev_conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
    learning_conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)
    try:
        users = await dev_conn.fetch('''
            SELECT chat_id, active_days_total, stats_reset_date
            FROM development.user_state
            WHERE active_days_total > 0
        ''')
        log_rows = await learning_conn.fetch('''
            SELECT chat_id, array_agg(DISTINCT activity_date ORDER BY activity_date) AS dates
            FROM public.activity_log
            GROUP BY chat_id
        ''')
        dates_by_chat_id = {r['chat_id']: r['dates'] for r in log_rows}

        buckets = Counter()
        deltas = []
        ambiguous = []

        for u in users:
            chat_id = u['chat_id']
            reset_date = u['stats_reset_date']
            all_dates = dates_by_chat_id.get(chat_id, [])

            relevant_dates = [d for d in all_dates if reset_date is None or d > reset_date]
            log_days = len(relevant_dates)
            diff = u['active_days_total'] - log_days

            if reset_date is not None and reset_date in all_dates:
                ambiguous.append((chat_id, reset_date, u['active_days_total'], log_days))
                continue  # не в confident-бакет — см. докстринг

            if diff > 0:
                buckets['counter > log'] += 1
            elif diff < 0:
                buckets['log > counter'] += 1
            else:
                buckets['match'] += 1
            deltas.append((chat_id, u['active_days_total'], log_days, diff))

        deltas.sort(key=lambda row: abs(row[3]), reverse=True)

        print(f"Проверено пользователей (active_days_total > 0): {len(users)}")
        print(f"Однозначных: {len(deltas)} | Неоднозначных (активность в день сброса): {len(ambiguous)}\n")

        print("Бакеты (однозначные):")
        for label, count in buckets.most_common():
            print(f"  {label:<14} {count}")

        if deltas:
            print(f"\nТоп-20 по модулю дельты:")
            print(f"{'chat_id':>12} | {'counter':>7} | {'log':>7} | {'diff':>6}")
            print("-" * 42)
            for chat_id, total, log_days, diff in deltas[:20]:
                print(f"{chat_id:>12} | {total:>7} | {log_days:>7} | {diff:>6}")

        if ambiguous:
            print(f"\nНеоднозначные (активность ровно в день stats_reset_date, исключены из бакетов):")
            print(f"{'chat_id':>12} | {'reset_date':>10} | {'counter':>7} | {'log(>reset)':>11}")
            print("-" * 48)
            for chat_id, reset_date, total, log_days in ambiguous[:20]:
                print(f"{chat_id:>12} | {str(reset_date):>10} | {total:>7} | {log_days:>11}")
            if len(ambiguous) > 20:
                print(f"  ... и ещё {len(ambiguous) - 20}")

        return 0
    finally:
        await dev_conn.close()
        await learning_conn.close()


if __name__ == "__main__":
    code = asyncio.run(dry_run())
    sys.exit(code)
