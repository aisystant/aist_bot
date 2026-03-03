"""
Миграция 006: Создание schema development + таблицы user_events

Event Store — слой Events цифрового двойника (Events → State → Views).
Архитектура: DP.ARCH.003, «Структура данных ЦД в Neon» — Фаза 0, §6.

Принцип: user_events — append-only лог ВСЕХ действий пользователя.
Никогда не UPDATE/DELETE. Партиционировано по месяцам.

Зависимости: нет (не меняет существующие таблицы).

Запуск:
    python -m db.migrations.006_create_development_schema
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Создание schema development и таблицы user_events"""
    print("Подключение к базе данных...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        # 1. Schema
        print("Создание schema development...")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS development")

        # 2. Проверка существования таблицы
        exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'development'
                  AND table_name   = 'user_events'
            )
        """)

        if exists:
            print("Таблица development.user_events уже существует — пропускаем создание")
        else:
            print("Создание таблицы development.user_events (partitioned)...")
            await conn.execute('''
                CREATE TABLE development.user_events (
                    id          BIGSERIAL,
                    user_id     BIGINT    NOT NULL,
                    event_type  TEXT      NOT NULL,
                    source      TEXT      NOT NULL,
                    payload     JSONB     NOT NULL DEFAULT '{}',
                    confidence  REAL               DEFAULT 1.0,
                    skill_ids   TEXT[]             DEFAULT '{}',
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                    PRIMARY KEY (id, created_at)
                ) PARTITION BY RANGE (created_at)
            ''')
            print("Таблица development.user_events создана")

        # 3. Партиции — текущий + следующие 2 месяца
        partitions = [
            ("user_events_2026_03", "2026-03-01", "2026-04-01"),
            ("user_events_2026_04", "2026-04-01", "2026-05-01"),
            ("user_events_2026_05", "2026-05-01", "2026-06-01"),
        ]

        for name, start, end in partitions:
            part_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'development'
                      AND table_name   = $1
                )
            """, name)

            if not part_exists:
                await conn.execute(f"""
                    CREATE TABLE development.{name}
                        PARTITION OF development.user_events
                        FOR VALUES FROM ('{start}') TO ('{end}')
                """)
                print(f"  Партиция {name} создана")
            else:
                print(f"  Партиция {name} уже существует")

        # 4. Индексы (применяются ко всем партициям)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_user_time
                ON development.user_events (user_id, created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_type
                ON development.user_events (event_type, created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_skills
                ON development.user_events USING GIN (skill_ids)
        """)
        print("Индексы созданы")

        # 5. Проверка
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM development.user_events"
        )
        print(f"Проверка: SELECT COUNT(*) = {count} (ожидается 0)")

        print("✅ Миграция 006 завершена")

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(migrate())
