"""
Миграция 053: interlocutor_stats — счётчики выпуска А (РП-262 Ф10).

Один ряд на (день, класс исхода): сколько ходов получили ответ агента, сколько раз не было пропуска,
сколько повторов, «недоступно», ошибок, и сумма времени ответа для среднего. Журнал Railway хранит
только текущий запуск бота, поэтому недельная доля сбоев считается по этой таблице.
Тексты вопросов, ответы, пропуски и номера чатов сюда не пишутся.

Колонки:
  day        — дата (UTC)
  kind       — класс исхода: turn, answer, agent_message, no_pass, retry, unavailable, error,
               forget_forgotten, forget_refused, forget_failed
  n          — сколько раз
  total_ms   — сумма времени ответа агента, мс (для хода и ответа)

Запуск вручную:
    python -m db.migrations.053_wp262_f10_interlocutor_stats
"""

import asyncio

import asyncpg


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'interlocutor_stats'
            )
            """
        )
        if exists:
            return False
        await conn.execute(
            """
            CREATE TABLE public.interlocutor_stats (
                day DATE NOT NULL,
                kind TEXT NOT NULL,
                n BIGINT NOT NULL DEFAULT 0,
                total_ms BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (day, kind)
            )
            """
        )
        return True


async def main() -> None:
    from db.connection import get_pool

    created = await migrate_if_needed(await get_pool())
    print("interlocutor_stats created" if created else "interlocutor_stats already exists")


if __name__ == "__main__":
    asyncio.run(main())
