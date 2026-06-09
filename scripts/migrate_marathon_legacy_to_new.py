"""
Миграция legacy-пользователей марафона на новый движок WP-330.

Проблема: старый движок (send_scheduled_topic + completed_topics/current_topic_index)
шлёт застрявшие дни повторно каждое утро, потому что idempotency-ключ включает дату.

Этот скрипт:
1. Находит всех active marathon-пользователей без записи в learning.marathon_progress
2. Вычисляет их текущий день по legacy-формуле
3. Создаёт marathon_progress и заполняет marathon_queue на 14 дней

Запуск:
    python -m scripts.migrate_marathon_legacy_to_new --dry-run
    python -m scripts.migrate_marathon_legacy_to_new

Выход:
    - Список обработанных пользователей
    - Итоговая статистика
    - Код возврата 0 при успехе, 1 при ошибках
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATABASE_URL, LEARNING_URL, MOSCOW_TZ


# Копия legacy-логики из core/topics.py::get_marathon_day
# чтобы не тащить зависимости от TOPICS/yaml.
def _legacy_marathon_day(start_date, current_topic_index: int = 0, max_days: int = 14) -> int:
    if not start_date:
        return (current_topic_index // 2) + 1 if current_topic_index > 0 else 1

    today = datetime.now(MOSCOW_TZ).date()
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    days_passed = (today - start_date).days
    return min(days_passed + 1, max_days)


# Парсинг completed_topics (может быть list или JSON-строка)
def _parse_completed_topics(raw):
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return []
    return []


FIND_LEGACY_USERS_SQL = """
SELECT s.chat_id,
       s.schedule_time,
       s.marathon_start_date,
       s.current_topic_index,
       s.completed_topics,
       s.language
FROM development.user_state s
WHERE s.marathon_status = 'active'
  AND s.onboarding_completed = TRUE
  AND s.bot_blocked IS NOT TRUE
ORDER BY s.chat_id
"""


async def _enqueue_day_items(conn, user_id: int, day_number: int, scheduled_at: datetime):
    """Локальная копия db.queries.marathon_newcomer.enqueue_day_items без зависимостей."""
    await conn.execute(
        """INSERT INTO learning.marathon_queue
           (user_id, day_number, content_type, scheduled_at, content_text)
           VALUES
           ($1, $2, 'lesson_practice', $3, NULL),
           ($1, $2, 'checkin',         $3 + INTERVAL '14 hours', NULL)
           ON CONFLICT (user_id, day_number, content_type) DO UPDATE SET
               content_text = EXCLUDED.content_text,
               scheduled_at = EXCLUDED.scheduled_at,
               status = CASE WHEN marathon_queue.status = 'sent' THEN 'sent' ELSE 'pending' END,
               sent_at = CASE WHEN marathon_queue.status = 'sent' THEN marathon_queue.sent_at ELSE NULL END,
               error = NULL,
               updated_at = NOW()""",
        user_id, day_number, scheduled_at,
    )


async def main():
    parser = argparse.ArgumentParser(description="Migrate legacy marathon users to WP-330 new engine")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be migrated")
    args = parser.parse_args()

    if not DATABASE_URL or not LEARNING_URL:
        print("ERROR: DATABASE_URL and LEARNING_URL must be set", file=sys.stderr)
        return 1

    dry_run_label = "[DRY-RUN] " if args.dry_run else ""
    print(f"{dry_run_label}Starting legacy marathon migration to WP-330 new engine")

    bot_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    learning_pool = await asyncpg.create_pool(LEARNING_URL, min_size=1, max_size=2)

    try:
        # 1. Найти legacy-пользователей (исключая тех, кто уже в новом движке)
        async with bot_pool.acquire() as conn:
            legacy_users = await conn.fetch(FIND_LEGACY_USERS_SQL)

        if not legacy_users:
            print(f"{dry_run_label}No legacy marathon users found. Nothing to migrate.")
            return 0

        # Cross-DB фильтр: у кого уже есть marathon_progress
        async with learning_pool.acquire() as lconn:
            progress_rows = await lconn.fetch(
                "SELECT user_id FROM learning.marathon_progress WHERE user_id = ANY($1::bigint[])",
                [r["chat_id"] for r in legacy_users],
            )
        newcomer_ids = {r["user_id"] for r in progress_rows}
        legacy_users = [r for r in legacy_users if r["chat_id"] not in newcomer_ids]

        if not legacy_users:
            print(f"{dry_run_label}No legacy marathon users found (all already migrated).")
            return 0

        print(f"{dry_run_label}Found {len(legacy_users)} legacy marathon user(s) to migrate")

        now = datetime.now(MOSCOW_TZ)
        processed = []
        errors = []

        for row in legacy_users:
            chat_id = row["chat_id"]
            schedule_time = row["schedule_time"] or "04:00"
            start_date = row["marathon_start_date"]
            current_topic_index = row["current_topic_index"] or 0
            completed_topics = _parse_completed_topics(row["completed_topics"])

            # Вычисляем текущий день
            old_day = _legacy_marathon_day(start_date, current_topic_index)
            old_day = max(1, min(14, old_day))

            # Учитываем completed_topics: если пройдено больше дней, чем вычислилось по дате,
            # берём максимум (defensive).
            days_from_completed = max(1, len(completed_topics) // 2)
            old_day = max(old_day, days_from_completed)
            old_day = min(old_day, 14)

            print(
                f"{dry_run_label}user={chat_id} "
                f"start_date={start_date} "
                f"current_topic_index={current_topic_index} "
                f"completed={len(completed_topics)} "
                f"→ old_day={old_day}"
            )

            if args.dry_run:
                processed.append({
                    "chat_id": chat_id,
                    "old_day": old_day,
                    "schedule_time": schedule_time,
                })
                continue

            try:
                async with learning_pool.acquire() as lconn:
                    # Создать/обновить marathon_progress
                    await lconn.execute(
                        """INSERT INTO learning.marathon_progress
                           (user_id, status, current_day, started_at, updated_at)
                           VALUES ($1, 'active', $2, $3, NOW())
                           ON CONFLICT (user_id) DO UPDATE SET
                               status = EXCLUDED.status,
                               current_day = EXCLUDED.current_day,
                               started_at = EXCLUDED.started_at,
                               updated_at = NOW()""",
                        chat_id, old_day, now,
                    )

                    # Заполнить очередь на 14 дней
                    sched_h, sched_m = map(int, schedule_time.split(":"))
                    tomorrow_sched = datetime(
                        now.year, now.month, now.day, sched_h, sched_m, 0, tzinfo=now.tzinfo
                    ) + timedelta(days=1)

                    for day in range(1, 15):
                        if day == old_day:
                            scheduled = now + timedelta(minutes=5)
                        elif day < old_day:
                            scheduled = now - timedelta(hours=1)
                        else:
                            scheduled = tomorrow_sched + timedelta(days=day - old_day - 1)

                        await _enqueue_day_items(lconn, chat_id, day, scheduled)

                    # Пометить пройденные дни как sent
                    if old_day > 1:
                        await lconn.execute(
                            """UPDATE learning.marathon_queue
                               SET status='sent', sent_at=NOW(), updated_at=NOW()
                               WHERE user_id=$1 AND day_number < $2 AND status='pending'""",
                            chat_id, old_day,
                        )

                processed.append({"chat_id": chat_id, "old_day": old_day})
                print(f"  ✓ migrated user={chat_id} to current_day={old_day}")

            except Exception as e:
                errors.append({"chat_id": chat_id, "error": str(e)})
                print(f"  ✗ failed user={chat_id}: {e}", file=sys.stderr)

        print(f"\n{dry_run_label}Summary:")
        print(f"  legacy users found: {len(legacy_users)}")
        print(f"  processed:          {len(processed)}")
        print(f"  errors:             {len(errors)}")

        if errors:
            for err in errors:
                print(f"  - user={err['chat_id']}: {err['error']}", file=sys.stderr)
            return 1

        return 0

    finally:
        await bot_pool.close()
        await learning_pool.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
