#!/usr/bin/env python3
"""
WP-218 Ф7.4 — однократный cleanup: merge users.id строки в ory_id строки в digital_twins.

Проблема: у некоторых пользователей в digital_twins ДВА ряда:
  - key = users.id (UUID, внутренний FK бота)
  - key = users.ory_id (Ory UUID, канонический)

Каждый ряд содержит разные секции 2_collected (исторически разные writers).
Этот скрипт: merge данных из users.id ряда → ory_id ряд, затем удаляет users.id ряд.

Запуск:
    DATABASE_URL=<bot_data_url> python3 scripts/fix_dt_identity_keys.py [--dry-run]

Критерий готовности после запуска:
    SELECT user_id, COUNT(*) FROM digital_twins GROUP BY user_id HAVING COUNT(*) > 1;
    → 0 рядов
"""

import argparse
import asyncio
import json
import os
import sys

import asyncpg


async def main(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(db_url)
    try:
        # Шаг 1: найти пары (users.id ряд, ory_id ряд) одного человека
        pairs = await conn.fetch("""
            SELECT
                users_row.user_id AS users_id_key,
                ory_row.user_id   AS ory_id_key,
                u.telegram_id
            FROM public.users u
            JOIN public.digital_twins users_row ON users_row.user_id = u.id::text
            JOIN public.digital_twins ory_row   ON ory_row.user_id   = u.ory_id::text
            WHERE u.ory_id IS NOT NULL
        """)

        print(f"Найдено пар для merge: {len(pairs)}")
        if not pairs:
            print("Нет дублей — ничего делать не нужно.")
            return

        for p in pairs:
            print(f"  telegram_id={p['telegram_id']}: {p['users_id_key']} → {p['ory_id_key']}")

        if dry_run:
            print("\n[DRY RUN] Изменения не применены.")
            return

        async with conn.transaction():
            # Шаг 2: merge 2_collected из users.id ряда в ory_id ряд
            # COALESCE(ory_row.data->'2_collected', '{}') || COALESCE(users_row.data->'2_collected', '{}')
            # jsonb || берёт правый операнд для перекрывающихся ключей — ory_id ряд приоритетнее
            merge_result = await conn.execute("""
                UPDATE public.digital_twins AS ory_row
                SET data = jsonb_build_object(
                    '2_collected',
                    COALESCE(users_row.data->'2_collected', '{}'::jsonb)
                    || COALESCE(ory_row.data->'2_collected', '{}'::jsonb)
                ),
                updated_at = NOW()
                FROM public.digital_twins AS users_row
                JOIN public.users u ON u.id::text = users_row.user_id
                WHERE ory_row.user_id = u.ory_id::text
                  AND u.ory_id IS NOT NULL
            """)
            print(f"Merge: {merge_result}")

            # Шаг 3: удалить users.id ряды
            delete_result = await conn.execute("""
                DELETE FROM public.digital_twins
                WHERE user_id IN (
                    SELECT u.id::text FROM public.users u
                    WHERE u.ory_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM public.digital_twins dt
                          WHERE dt.user_id = u.ory_id::text
                      )
                      AND EXISTS (
                          SELECT 1 FROM public.digital_twins dt2
                          WHERE dt2.user_id = u.id::text
                      )
                )
            """)
            print(f"Удалено users.id рядов: {delete_result}")

        # Шаг 4: верификация
        remaining = await conn.fetch("""
            SELECT user_id, COUNT(*) as cnt
            FROM public.digital_twins
            GROUP BY user_id
            HAVING COUNT(*) > 1
        """)
        if remaining:
            print(f"ПРЕДУПРЕЖДЕНИЕ: осталось дублей: {len(remaining)}")
            for r in remaining:
                print(f"  user_id={r['user_id']} cnt={r['cnt']}")
        else:
            print("✓ Дублей нет — cleanup успешен.")

        # Показать итоговое состояние Церена
        tseren = await conn.fetch("""
            SELECT dt.user_id,
                   CASE WHEN u.id::text = dt.user_id THEN 'users.id'
                        WHEN u.ory_id::text = dt.user_id THEN 'ory_id' END AS key_type,
                   dt.data->'2_collected' ? '2_1_account' AS has_engagement,
                   dt.data->'2_collected' ? '2_7_iwe' AS has_iwe,
                   dt.data->'2_collected' ? '2_8_ecosystem' AS has_ecosystem,
                   dt.data->'2_collected' ? '2_9_knowledge' AS has_knowledge
            FROM public.digital_twins dt
            JOIN public.users u ON u.id::text = dt.user_id OR u.ory_id::text = dt.user_id
            WHERE u.telegram_id = 74892489
        """)
        print("\nСостояние Церена после cleanup:")
        for r in tseren:
            print(f"  user_id={r['user_id']} type={r['key_type']} "
                  f"engagement={r['has_engagement']} iwe={r['has_iwe']} "
                  f"ecosystem={r['has_ecosystem']} knowledge={r['has_knowledge']}")

    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix digital_twins identity key duplicates")
    parser.add_argument("--dry-run", action="store_true", help="Show pairs without applying changes")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
