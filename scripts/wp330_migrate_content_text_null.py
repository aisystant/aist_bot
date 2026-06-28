#!/usr/bin/env python3
"""WP-330 Ф10 С5: rollout long_complex split на ВСЕХ активных пилотов.

Цель
----
Для всех `marathon_progress.status='active'` пилотов в БД установить
`content_text = NULL` в pending `lesson_practice` записях очереди
для дней `day_number > current_day`. После migration следующий тик
`_process_marathon_queue` увидит `content_text IS NULL` и сформирует
split-сообщение (lesson_full + кнопка → practice_full).

Защита от race с running cron — `pg_advisory_xact_lock(3302026)`.
Lock автоматически снимется при COMMIT/ROLLBACK транзакции.

Backup
------
До UPDATE создаётся таблица
`learning.marathon_queue_backup_wp330_f10_c5_<YYYYMMDD>` с колонками
`(id, content_text, updated_at)` — только то, что нужно для отката.
Rollback: см. конец файла, секция «Rollback procedure».

Запуск
------
    railway run --environment pilot      python3 scripts/wp330_migrate_content_text_null.py --dry-run
    railway run --environment pilot      python3 scripts/wp330_migrate_content_text_null.py --apply
    railway run --environment production python3 scripts/wp330_migrate_content_text_null.py --dry-run
    railway run --environment production python3 scripts/wp330_migrate_content_text_null.py --apply

Idempotent: повторный запуск с `--apply` снова `UPDATE ... SET content_text = NULL`
на тех же строках — изменений 0, backup-таблица уже существует → IF NOT EXISTS пропустит CREATE.

Связанные коммиты С1+С2 (split-инфраструктура на pilot):
- 3e892d1 feat(WP-330 Ф10): расширенный формат урока + faq_hint (С1)
- c5820ea feat(WP-330 Ф10 С2): split + practice nudge + curriculum sync

Сессия С5: sessions/2026-05/2026-05-30-34-wp330-c5-rollout-plan/

Rollback procedure
------------------
    BEGIN;
    SELECT pg_advisory_xact_lock(3302026);
    UPDATE learning.marathon_queue AS q
       SET content_text = b.content_text, updated_at = NOW()
      FROM learning.marathon_queue_backup_wp330_f10_c5_<YYYYMMDD> AS b
     WHERE q.id = b.id
       AND q.content_text IS NULL
       AND q.status = 'pending';   -- не трогаем уже отправленные split-сообщения
    -- проверить result count = ожидаемое число строк, затем COMMIT (или ROLLBACK)
    COMMIT;
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

import asyncpg

ADVISORY_LOCK_KEY = 3302026  # WP-330 С5 — уникальный ключ для xact-lock
BACKUP_TABLE_SUFFIX = datetime.utcnow().strftime("%Y%m%d")
BACKUP_TABLE = f"learning.marathon_queue_backup_wp330_f10_c5_{BACKUP_TABLE_SUFFIX}"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Показать что будет изменено, без записи.")
    mode.add_argument("--apply", action="store_true", help="Создать backup + выполнить UPDATE.")
    args = parser.parse_args()

    db_url = os.environ.get("LEARNING_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: ни LEARNING_URL, ни DATABASE_URL не заданы.", file=sys.stderr)
        return 2

    print(f"=== WP-330 С5 migration ({'DRY-RUN' if args.dry_run else 'APPLY'}) ===")
    print(f"DB: {_redact_url(db_url)}")
    print(f"Backup table: {BACKUP_TABLE}")
    print(f"Advisory lock key: {ADVISORY_LOCK_KEY}")
    print()

    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
            print(f"✓ Advisory lock acquired (xact, key={ADVISORY_LOCK_KEY}).")

            preview = await _preview(conn)
            print(f"Активных пилотов: {preview['active_users']}")
            print(f"Pending lesson_practice (day > current_day): {preview['rows_to_update']}")
            print(f"  из них content_text IS NOT NULL (нуждаются в migration): {preview['rows_legacy']}")
            print(f"  из них content_text IS NULL (уже split): {preview['rows_already_split']}")

            if preview["rows_to_update"] == 0:
                print("→ Нечего мигрировать. Выход.")
                return 0

            if args.dry_run:
                sample = await _sample(conn, limit=10)
                print("\nSample (до 10 строк):")
                for row in sample:
                    flag = "LEGACY" if row["content_text"] is not None else "SPLIT"
                    print(
                        f"  id={row['id']:>6} user={row['user_id']:>10} day={row['day_number']:>2} "
                        f"scheduled_at={row['scheduled_at']} [{flag}]"
                    )
                print("\nDRY-RUN: транзакция будет откачена.")
                raise _DryRunRollback

            backup_count = await _create_backup(conn)
            print(f"\n✓ Backup table {BACKUP_TABLE} создан ({backup_count} строк).")

            updated = await _apply_update(conn)
            print(f"✓ UPDATE выполнен: {updated} строк теперь content_text=NULL.")

        if not args.dry_run:
            verify = await _verify(conn)
            print(f"\n✓ Verify post-commit: pending split={verify['split']}, pending legacy={verify['legacy']}.")
            print(f"\n=== APPLY OK ===")
            print(f"Rollback при необходимости:")
            print(_rollback_sql())
        return 0

    except _DryRunRollback:
        print("\n=== DRY-RUN OK (no changes) ===")
        return 0
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        await conn.close()
        await asyncio.sleep(0)


class _DryRunRollback(Exception):
    """Signal к откату транзакции после dry-run preview."""


def _redact_url(url: str) -> str:
    # postgres://user:pass@host:port/db → postgres://user:***@host:port/db
    import re
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", url)


async def _preview(conn: asyncpg.Connection) -> dict:
    row = await conn.fetchrow("""
        WITH active AS (
            SELECT user_id, current_day FROM learning.marathon_progress WHERE status = 'active'
        ),
        target AS (
            SELECT q.id, q.content_text
              FROM learning.marathon_queue q
              JOIN active a ON a.user_id = q.user_id
             WHERE q.status = 'pending'
               AND q.content_type = 'lesson_practice'
               AND q.day_number > a.current_day
        )
        SELECT
            (SELECT count(*) FROM active) AS active_users,
            (SELECT count(*) FROM target) AS rows_to_update,
            (SELECT count(*) FROM target WHERE content_text IS NOT NULL) AS rows_legacy,
            (SELECT count(*) FROM target WHERE content_text IS NULL) AS rows_already_split
    """)
    return dict(row)


async def _sample(conn: asyncpg.Connection, limit: int = 10) -> list:
    rows = await conn.fetch("""
        SELECT q.id, q.user_id, q.day_number, q.scheduled_at, q.content_text
          FROM learning.marathon_queue q
          JOIN learning.marathon_progress p ON p.user_id = q.user_id
         WHERE p.status = 'active'
           AND q.status = 'pending'
           AND q.content_type = 'lesson_practice'
           AND q.day_number > p.current_day
         ORDER BY q.user_id, q.day_number
         LIMIT $1
    """, limit)
    return [dict(r) for r in rows]


async def _create_backup(conn: asyncpg.Connection) -> int:
    # IF NOT EXISTS → идемпотентность повторных запусков в один день.
    create_backup_sql = (  # nosec B608 (backup table name is a date-qualified constant)
        "CREATE TABLE IF NOT EXISTS " + BACKUP_TABLE + " (\n"  # nosec B608 (backup table name is a date-qualified constant)
        "    id           bigint PRIMARY KEY,\n"
        "    content_text text,\n"
        "    backed_up_at timestamptz NOT NULL DEFAULT NOW()\n"
        ")"
    )
    await conn.execute(create_backup_sql)
    backup_insert_sql = (  # nosec B608 (backup table name is a date-qualified constant)
        "INSERT INTO " + BACKUP_TABLE + " (id, content_text)\n"  # nosec B608 (backup table name is a date-qualified constant)
        "SELECT q.id, q.content_text\n"
        "  FROM learning.marathon_queue q\n"
        "  JOIN learning.marathon_progress p ON p.user_id = q.user_id\n"
        " WHERE p.status = 'active'\n"
        "   AND q.status = 'pending'\n"
        "   AND q.content_type = 'lesson_practice'\n"
        "   AND q.day_number > p.current_day\n"
        "ON CONFLICT (id) DO NOTHING"
    )
    result = await conn.execute(backup_insert_sql)
    # asyncpg возвращает «INSERT 0 N»
    return int(result.split()[-1])


async def _apply_update(conn: asyncpg.Connection) -> int:
    result = await conn.execute("""
        UPDATE learning.marathon_queue q
           SET content_text = NULL, updated_at = NOW()
          FROM learning.marathon_progress p
         WHERE p.user_id = q.user_id
           AND p.status = 'active'
           AND q.status = 'pending'
           AND q.content_type = 'lesson_practice'
           AND q.day_number > p.current_day
           AND q.content_text IS NOT NULL
    """)
    return int(result.split()[-1])


async def _verify(conn: asyncpg.Connection) -> dict:
    row = await conn.fetchrow("""
        SELECT
            count(*) FILTER (WHERE q.content_text IS NULL)     AS split,
            count(*) FILTER (WHERE q.content_text IS NOT NULL) AS legacy
          FROM learning.marathon_queue q
          JOIN learning.marathon_progress p ON p.user_id = q.user_id
         WHERE p.status = 'active'
           AND q.status = 'pending'
           AND q.content_type = 'lesson_practice'
           AND q.day_number > p.current_day
    """)
    return dict(row)


def _rollback_sql() -> str:
    return (
        "BEGIN;\n"
        f"    SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});\n"
        "    UPDATE learning.marathon_queue AS q\n"
        "       SET content_text = b.content_text, updated_at = NOW()\n"
        "      FROM " + BACKUP_TABLE + " AS b\n"  # nosec B608 (backup table name is a date-qualified constant)
        "     WHERE q.id = b.id\n"
        "       AND q.content_text IS NULL\n"
        "       AND q.status = 'pending';  -- не трогаем уже отправленные split-сообщения\n"
        "    -- check affected count, затем COMMIT (или ROLLBACK)\n"
        "COMMIT;"
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
