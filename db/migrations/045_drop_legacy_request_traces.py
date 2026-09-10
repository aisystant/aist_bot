"""
Миграция 045: удаление legacy public.request_traces из main pool (WP-562).

Контекст: писатель и читатели трассировки запросов переехали в health-БД
коммитом a3fea232 (WP-253 G4, 8 мая 2026) с явным планом «soak 24h, потом
DROP bot_data.request_traces». Соскока не случилось 4 месяца — таблица в
main pool простаивала (0 строк с мая). db/models.py.create_tables() больше
не создаёт её (эта миграция и db/models.py правились в одном коммите);
единственный оставшийся потребитель (GDPR-очистка в db/queries/profile.py)
тоже правился в том же коммите, ловля UndefinedTableError там уже была на
случай отсутствия таблицы. Health-БД копию (create_tables_health) эта
миграция не трогает — она активно используется.

Запуск вручную (prod работает с SKIP_DB_MIGRATIONS=true, поэтому обычный
деплой эту миграцию не применит — нужен явный ручной прогон против main pool):
    python -m db.migrations.045_drop_legacy_request_traces
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
                  AND table_name = 'request_traces'
            )
            """
        )
        if not exists:
            return False

        await conn.execute('DROP TABLE public.request_traces')
    return True


if __name__ == "__main__":
    from config import DATABASE_URL

    async def run():
        pool = await asyncpg.create_pool(DATABASE_URL)
        dropped = await migrate_if_needed(pool)
        print(f"Migration 045: {'request_traces dropped' if dropped else 'already absent'}")
        await pool.close()

    asyncio.run(run())
