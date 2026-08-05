#!/usr/bin/env python3
"""
WP-117 Ф-stopgap: подавить уже поставленные в очередь achievement-нуджи.

Меняет status 'queued' → 'suppressed' с reason='wp117-achievement-stopgap'
для записей development.notification_queue, у которых dedup_key соответствует
achievement-нуджам (nudge_sessions_N, nudge_active_days_N, nudge_stage_reached_N).

Не удаляет записи — suppressed не занимает cooldown/cap и оставляет аудит-след.

Run:
    DATABASE_URL=<url> python3 scripts/wp117_suppress_queued_achievements.py [--dry-run]

Safety:
    - Перед запуском остановить/поставить на паузу drain Доставщика,
      иначе есть гонка: drain может забрать queued-строку первым.
    - Скрипт использует одну транзакцию и RETURNING id — проверьте count.
"""

import argparse
import asyncio
import os
import sys

import asyncpg

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.nudge_policy import stopgap_dedup_key_pattern


REASON = "wp117-achievement-stopgap"


async def main(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    pool = await asyncpg.create_pool(db_url, statement_cache_size=0)
    try:
        async with pool.acquire() as conn:
            regex = stopgap_dedup_key_pattern()
            print(f"PostgreSQL regex: {regex}")

            if dry_run:
                count = await conn.fetchval(
                    """SELECT count(*)
                       FROM development.notification_queue
                       WHERE status = 'queued'
                         AND journal_type = 'nudge'
                         AND dedup_key ~ $1""",
                    regex,
                )
                print(f"DRY RUN: {count} queued achievement nudges would be suppressed")
                return

            rows = await conn.fetch(
                """UPDATE development.notification_queue
                   SET status = 'suppressed',
                       reason = $2,
                       attempts = attempts + 1
                   WHERE status = 'queued'
                     AND journal_type = 'nudge'
                     AND dedup_key ~ $1
                   RETURNING id, chat_id, dedup_key""",
                regex, REASON,
            )
            print(f"Suppressed {len(rows)} queued achievement nudges")
            for row in rows:
                print(f"  id={row['id']} chat_id={row['chat_id']} dedup_key={row['dedup_key']}")
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Suppress queued WP-117 achievement nudges"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
