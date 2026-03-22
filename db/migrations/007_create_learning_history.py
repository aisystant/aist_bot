"""
Миграция 007: Создать таблицу development.learning_history + trigger (WP-149).

Подход C (Event-sourced + Materialized Table):
  - user_events (append-only) = source of truth
  - learning_history (materialized) = типизированная проекция для быстрого чтения
  - trigger materialize_learning = автоматическая материализация при INSERT в user_events

Портной (MIM.SOP.001) читает learning_history для:
  - Шаг 3: выбор направления (depths_by_direction)
  - Шаг 4: bottleneck-first (MAX bloom WHERE passed)

dt_sync.py пишет 2_10_learning_history в ЦД.

Запуск:
    python -m db.migrations.007_create_learning_history
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Создать таблицу learning_history и trigger."""
    print("Подключение к базе данных...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        # ═══════════════════════════════════════════════════════════
        # 1. Таблица development.learning_history
        # ═══════════════════════════════════════════════════════════
        await conn.execute('CREATE SCHEMA IF NOT EXISTS development')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS development.learning_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                user_uuid UUID,

                -- Что прошёл
                program_id TEXT NOT NULL,
                module_id TEXT,
                direction SMALLINT NOT NULL,
                topic_id TEXT NOT NULL,
                bloom_level SMALLINT NOT NULL,
                cell_id TEXT,

                -- Результат
                score REAL,
                passed BOOLEAN NOT NULL DEFAULT FALSE,
                errors TEXT[] DEFAULT '{}',
                duration_min SMALLINT,

                -- Контекст сборки (от Портного, для аудита и шага 3 SOP.001)
                tailor_context JSONB,

                -- Мета
                source TEXT NOT NULL DEFAULT 'bot',
                event_id BIGINT NOT NULL,
                schema_version SMALLINT NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),

                -- Constraints
                CONSTRAINT chk_lh_direction CHECK (direction BETWEEN 1 AND 6),
                CONSTRAINT chk_lh_bloom CHECK (bloom_level BETWEEN 1 AND 5),
                CONSTRAINT chk_lh_score CHECK (
                    score IS NULL OR (score >= 0.0 AND score <= 1.0)
                ),
                CONSTRAINT chk_lh_source CHECK (
                    source IN ('bot', 'lms', 'club', 'web_app', 'iwe')
                ),
                CONSTRAINT uq_lh_event_id UNIQUE (event_id)
            )
        ''')
        print("✅ Таблица development.learning_history создана")

        # ═══════════════════════════════════════════════════════════
        # 2. Индексы
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_lh_user
            ON development.learning_history(user_id)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_lh_user_topic
            ON development.learning_history(user_id, topic_id)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_lh_user_dir
            ON development.learning_history(user_id, direction)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_lh_user_dir_passed
            ON development.learning_history(user_id, direction)
            WHERE passed = TRUE
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_lh_user_passed
            ON development.learning_history(user_id)
            WHERE passed = TRUE
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_lh_created
            ON development.learning_history(created_at)
        ''')
        print("✅ Индексы созданы (6 шт.)")

        # ═══════════════════════════════════════════════════════════
        # 3. Trigger function
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE OR REPLACE FUNCTION development.materialize_learning()
            RETURNS TRIGGER AS $$
            DECLARE
                _payload JSONB;
                _dir SMALLINT;
                _bloom SMALLINT;
                _score REAL;
                _errors TEXT[];
            BEGIN
                -- Guard: только learning_completed
                IF NEW.event_type != 'learning_completed' THEN
                    RETURN NEW;
                END IF;

                -- Guard: NULL или пустой payload
                IF NEW.payload IS NULL OR NEW.payload = '{}'::jsonb THEN
                    RAISE WARNING
                        '[learning_history] NULL/empty payload, event_id=%, user_id=%',
                        NEW.id, NEW.user_id;
                    RETURN NEW;
                END IF;

                _payload := NEW.payload;

                -- Извлечение с type-safe кастами
                BEGIN
                    _dir := (_payload->>'direction')::SMALLINT;
                    _bloom := (_payload->>'bloom_level')::SMALLINT;
                    _score := (_payload->>'score')::REAL;

                    -- Валидация диапазонов
                    IF _dir IS NULL OR _dir < 1 OR _dir > 6 THEN
                        RAISE WARNING
                            '[learning_history] invalid direction=%, event_id=%',
                            _dir, NEW.id;
                        RETURN NEW;
                    END IF;

                    IF _bloom IS NULL OR _bloom < 1 OR _bloom > 5 THEN
                        RAISE WARNING
                            '[learning_history] invalid bloom_level=%, event_id=%',
                            _bloom, NEW.id;
                        RETURN NEW;
                    END IF;

                    IF _score IS NOT NULL AND (_score < 0.0 OR _score > 1.0) THEN
                        RAISE WARNING
                            '[learning_history] invalid score=%, event_id=%',
                            _score, NEW.id;
                        RETURN NEW;
                    END IF;

                    -- Обязательные поля
                    IF _payload->>'topic_id' IS NULL
                       OR _payload->>'program_id' IS NULL THEN
                        RAISE WARNING
                            '[learning_history] missing required fields, event_id=%',
                            NEW.id;
                        RETURN NEW;
                    END IF;

                    -- Валидация source (дополнительно к CHECK)
                    IF NEW.source NOT IN (
                        'bot', 'lms', 'club', 'web_app', 'iwe'
                    ) THEN
                        RAISE WARNING
                            '[learning_history] invalid source=%, event_id=%',
                            NEW.source, NEW.id;
                        RETURN NEW;
                    END IF;

                    -- Извлечение errors с фильтрацией NULL/пустых
                    SELECT ARRAY_AGG(elem)
                    INTO _errors
                    FROM jsonb_array_elements_text(
                        COALESCE(_payload->'errors', '[]'::jsonb)
                    ) AS elem
                    WHERE elem IS NOT NULL AND elem != '';

                    -- INSERT в материализованную таблицу
                    INSERT INTO development.learning_history
                        (user_id, user_uuid, program_id, module_id,
                         direction, topic_id, bloom_level, cell_id,
                         score, passed, errors, duration_min,
                         tailor_context,
                         source, event_id, schema_version, created_at)
                    VALUES (
                        NEW.user_id,
                        NEW.user_uuid,
                        _payload->>'program_id',
                        _payload->>'module_id',
                        _dir,
                        _payload->>'topic_id',
                        _bloom,
                        _payload->>'cell_id',
                        _score,
                        COALESCE((_payload->>'passed')::BOOLEAN, FALSE),
                        COALESCE(_errors, '{}'::TEXT[]),
                        (_payload->>'duration_min')::SMALLINT,
                        _payload->'tailor_context',
                        NEW.source,
                        NEW.id,
                        COALESCE(
                            (_payload->>'_schema_version')::SMALLINT, 1
                        ),
                        NEW.created_at
                    );

                EXCEPTION WHEN OTHERS THEN
                    -- НЕ роняем транзакцию — event сохранится в user_events
                    RAISE WARNING
                        '[learning_history] materialization failed, event_id=%: %',
                        NEW.id, SQLERRM;
                END;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        ''')
        print("✅ Функция development.materialize_learning() создана")

        # ═══════════════════════════════════════════════════════════
        # 4. Trigger
        # ═══════════════════════════════════════════════════════════
        # Удалить старый trigger если существует
        await conn.execute('''
            DROP TRIGGER IF EXISTS trg_materialize_learning
            ON development.user_events
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_materialize_learning
                AFTER INSERT ON development.user_events
                FOR EACH ROW
                EXECUTE FUNCTION development.materialize_learning()
        ''')
        print("✅ Trigger trg_materialize_learning создан")

        # ═══════════════════════════════════════════════════════════
        # 5. Проверка
        # ═══════════════════════════════════════════════════════════
        count = await conn.fetchval(
            'SELECT COUNT(*) FROM development.learning_history'
        )
        print(f"✅ learning_history: {count} записей")

        # Replay существующих events (если есть learning_completed)
        existing = await conn.fetchval('''
            SELECT COUNT(*) FROM development.user_events
            WHERE event_type = 'learning_completed'
        ''')
        if existing > 0:
            print(f"⚠️  Найдено {existing} существующих learning_completed событий.")
            print("   Запускаю replay...")
            replayed = await conn.fetchval('''
                WITH inserted AS (
                    INSERT INTO development.learning_history
                        (user_id, user_uuid, program_id, module_id,
                         direction, topic_id, bloom_level, cell_id,
                         score, passed, errors, duration_min,
                         tailor_context,
                         source, event_id, schema_version, created_at)
                    SELECT
                        e.user_id,
                        e.user_uuid,
                        e.payload->>'program_id',
                        e.payload->>'module_id',
                        (e.payload->>'direction')::SMALLINT,
                        e.payload->>'topic_id',
                        (e.payload->>'bloom_level')::SMALLINT,
                        e.payload->>'cell_id',
                        (e.payload->>'score')::REAL,
                        COALESCE((e.payload->>'passed')::BOOLEAN, FALSE),
                        COALESCE(
                            ARRAY(
                                SELECT elem
                                FROM jsonb_array_elements_text(
                                    COALESCE(e.payload->'errors', '[]'::jsonb)
                                ) AS elem
                                WHERE elem IS NOT NULL AND elem != ''
                            ),
                            '{}'::TEXT[]
                        ),
                        (e.payload->>'duration_min')::SMALLINT,
                        e.payload->'tailor_context',
                        e.source,
                        e.id,
                        COALESCE(
                            (e.payload->>'_schema_version')::SMALLINT, 1
                        ),
                        e.created_at
                    FROM development.user_events e
                    WHERE e.event_type = 'learning_completed'
                      AND e.payload IS NOT NULL
                      AND e.payload != '{}'::jsonb
                      AND e.payload->>'topic_id' IS NOT NULL
                      AND e.payload->>'program_id' IS NOT NULL
                      AND (e.payload->>'direction')::SMALLINT BETWEEN 1 AND 6
                      AND (e.payload->>'bloom_level')::SMALLINT BETWEEN 1 AND 5
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING id
                )
                SELECT COUNT(*) FROM inserted
            ''')
            print(f"✅ Replay: {replayed} записей материализовано")

    finally:
        await conn.close()
    print("Миграция 007 завершена.")


if __name__ == '__main__':
    asyncio.run(migrate())
