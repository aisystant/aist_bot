"""
Миграция 044: internship_payment_checks — доставка приглашения в чат потока
после оплаты программы/резидентуры/семинара через Aisystant (WP-5).

Контекст: create_internship_payment() (purpose=INTERNSHIP) отдаёт пользователю
ссылку на оплату у Aisystant, но, в отличие от SEMINAR/WORKSHOP, оплата этого
вида НИКОГДА не бьёт вебхуком в наш бот — Aisystant ничего не присылает.
На старом боте (@SystemsSchool_bot) эта дыра закрыта поллингом через
threading.Timer + check-payment; здесь тот же поллинг переносится на
core.scheduler (async cron), т.к. threading.Timer несовместим с aiogram loop.

Колонки:
  id            — SERIAL PRIMARY KEY
  telegram_id   — кому слать результат
  aisystant_id  — для запроса check-payment/crm-course-passings
  payment_id    — id платежа от create-internship-payment (для check-payment)
  code          — код потока (для поиска chatLink после SUCCEEDED)
  course_name   — название на момент создания платежа (fallback, если поток
                  к моменту проверки уже не найдётся в crm-course-passings)
  lang          — язык сообщения пользователю
  status        — pending → succeeded / failed / gave_up
  attempts      — сколько раз проверяли check-payment
  created_at    — когда создан платёж
  checked_at    — когда последний раз проверяли

Запуск вручную:
    python -m db.migrations.044_internship_payment_checks
"""

import asyncio
import asyncpg


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'internship_payment_checks'
            )
            """
        )
        if exists:
            return False

        await conn.execute(
            """
            CREATE TABLE public.internship_payment_checks (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                aisystant_id TEXT NOT NULL,
                payment_id TEXT NOT NULL,
                code TEXT NOT NULL,
                course_name TEXT NOT NULL,
                lang TEXT NOT NULL DEFAULT 'ru',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                checked_at TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX idx_internship_payment_checks_payment_id
                ON public.internship_payment_checks (payment_id)
            """
        )
        await conn.execute(
            """
            CREATE INDEX idx_internship_payment_checks_pending
                ON public.internship_payment_checks (status, created_at)
                WHERE status = 'pending'
            """
        )
    return True


if __name__ == "__main__":
    from config import DATABASE_URL

    async def run():
        pool = await asyncpg.create_pool(DATABASE_URL)
        created = await migrate_if_needed(pool)
        print(f"Migration 044: {'internship_payment_checks created' if created else 'already exists'}")
        await pool.close()

    asyncio.run(run())
