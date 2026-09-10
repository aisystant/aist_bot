"""
Миграция 046: workshop_payments.product — метка прямой покупки Мастерской IWE.

Контекст: до этой миграции чат Мастерской открывался только через подсчёт
количества успешных оплат (2-я оплата в воронке Семинар → Мастерская).
Новый способ входа — прямая покупка Мастерской в один платёж, без Семинара.
Поле amount для этого не годится: Stars-платёж и рублёвый пишут разные
шкалы чисел в один и тот же столбец (4000 звёзд vs 8000 рублей за один
и тот же товар), поэтому нужен отдельный явный признак товара.

Колонка: product TEXT NULL. NULL = обычный шаг воронки (текущее поведение,
обратная совместимость). 'masterskaya_direct' = прямая покупка Мастерской.

Запуск вручную:
    python -m db.migrations.046_workshop_payments_product
"""

import asyncio
import asyncpg


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'workshop_payments'
                  AND column_name = 'product'
            )
            """
        )
        if exists:
            return False

        await conn.execute(
            """ALTER TABLE public.workshop_payments ADD COLUMN product TEXT"""
        )
    return True


if __name__ == "__main__":
    from config import DATABASE_URL

    async def run():
        pool = await asyncpg.create_pool(DATABASE_URL)
        created = await migrate_if_needed(pool)
        print(f"Migration 046: {'product column added' if created else 'already exists'}")
        await pool.close()

    asyncio.run(run())
