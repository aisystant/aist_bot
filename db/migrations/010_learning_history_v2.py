"""
Миграция 010: Расширение learning_history для модели v2 (WP-149, SOP.001 v3).

Изменения:
  - Новые колонки: element_id, element_type, area, impact_type, depth
  - Старые колонки (direction, topic_id, bloom_level) → nullable (backward compat)
  - Ослабление constraint chk_lh_direction (убираем, т.к. area 1-5 вместо direction 1-6)
  - Новый constraint chk_lh_area
  - Обновление trigger для schema_version=2
  - Новые индексы для element_id, area

Контекст:
  - 6 направлений → 5 областей (PD.FORM.081)
  - topic_id → element_id (M-xxx мемы, METHOD.xxx практики)
  - bloom_level → depth (1-3 для мемов, 1-4 для практик)
  - Добавлен impact_type (worldview/mastery)

Запуск:
    python -m db.migrations.010_learning_history_v2
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    """Расширить learning_history для v2."""
    print("Подключение к базе данных...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        # ═══════════════════════════════════════════════════════════
        # 1. Добавить новые колонки
        # ═══════════════════════════════════════════════════════════
        columns_to_add = [
            ("element_id", "TEXT"),
            ("element_type", "TEXT"),
            ("area", "SMALLINT"),
            ("impact_type", "TEXT"),
            ("depth", "SMALLINT"),
        ]

        for col_name, col_type in columns_to_add:
            exists = await conn.fetchval('''
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'development'
                      AND table_name = 'learning_history'
                      AND column_name = $1
                )
            ''', col_name)
            if not exists:
                await conn.execute(f'''
                    ALTER TABLE development.learning_history
                    ADD COLUMN {col_name} {col_type}
                ''')
                print(f"  ✅ Колонка {col_name} добавлена")
            else:
                print(f"  ⏭ Колонка {col_name} уже существует")

        # ═══════════════════════════════════════════════════════════
        # 2. Сделать старые колонки nullable
        # ═══════════════════════════════════════════════════════════
        for col in ['direction', 'topic_id', 'bloom_level']:
            await conn.execute(f'''
                ALTER TABLE development.learning_history
                ALTER COLUMN {col} DROP NOT NULL
            ''')
        print("  ✅ direction, topic_id, bloom_level → nullable")

        # ═══════════════════════════════════════════════════════════
        # 3. Обновить constraints
        # ═══════════════════════════════════════════════════════════
        # Убрать старый constraint на direction (1-6)
        await conn.execute('''
            ALTER TABLE development.learning_history
            DROP CONSTRAINT IF EXISTS chk_lh_direction
        ''')
        print("  ✅ chk_lh_direction удалён")

        # Убрать старый constraint на bloom (1-5)
        await conn.execute('''
            ALTER TABLE development.learning_history
            DROP CONSTRAINT IF EXISTS chk_lh_bloom
        ''')
        print("  ✅ chk_lh_bloom удалён")

        # Новые constraints
        await conn.execute('''
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_lh_area'
                ) THEN
                    ALTER TABLE development.learning_history
                    ADD CONSTRAINT chk_lh_area CHECK (
                        area IS NULL OR (area BETWEEN 1 AND 5)
                    );
                END IF;
            END $$
        ''')

        await conn.execute('''
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_lh_depth'
                ) THEN
                    ALTER TABLE development.learning_history
                    ADD CONSTRAINT chk_lh_depth CHECK (
                        depth IS NULL OR (depth BETWEEN 1 AND 4)
                    );
                END IF;
            END $$
        ''')

        await conn.execute('''
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_lh_impact_type'
                ) THEN
                    ALTER TABLE development.learning_history
                    ADD CONSTRAINT chk_lh_impact_type CHECK (
                        impact_type IS NULL
                        OR impact_type IN ('worldview', 'mastery')
                    );
                END IF;
            END $$
        ''')

        await conn.execute('''
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_lh_element_type'
                ) THEN
                    ALTER TABLE development.learning_history
                    ADD CONSTRAINT chk_lh_element_type CHECK (
                        element_type IS NULL
                        OR element_type IN ('meme', 'practice', 'cell')
                    );
                END IF;
            END $$
        ''')
        print("  ✅ Новые constraints добавлены (area, depth, impact_type, element_type)")

        # ═══════════════════════════════════════════════════════════
        # 4. Новые индексы
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_lh_user_element
            ON development.learning_history(user_id, element_id)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_lh_user_area
            ON development.learning_history(user_id, area)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_lh_user_element_passed
            ON development.learning_history(user_id, element_id)
            WHERE passed = TRUE
        ''')
        print("  ✅ Новые индексы созданы (3 шт.)")

        # ═══════════════════════════════════════════════════════════
        # 5. Обновить trigger function для schema_version=2
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE OR REPLACE FUNCTION development.materialize_learning()
            RETURNS TRIGGER AS $$
            DECLARE
                _payload JSONB;
                _schema SMALLINT;
            BEGIN
                IF NEW.event_type != 'learning_completed' THEN
                    RETURN NEW;
                END IF;

                IF NEW.payload IS NULL OR NEW.payload = '{}'::jsonb THEN
                    RAISE WARNING
                        '[learning_history] NULL/empty payload, event_id=%, user_id=%',
                        NEW.id, NEW.user_id;
                    RETURN NEW;
                END IF;

                _payload := NEW.payload;
                _schema := COALESCE(
                    (_payload->>'_schema_version')::SMALLINT, 1
                );

                BEGIN
                    IF _schema >= 2 THEN
                        -- v2: element-based (5 областей, мемы/практики)
                        INSERT INTO development.learning_history
                            (user_id, user_uuid, program_id,
                             element_id, element_type, area, impact_type, depth,
                             score, passed, errors, duration_min,
                             tailor_context,
                             source, event_id, schema_version, created_at)
                        VALUES (
                            NEW.user_id,
                            NEW.user_uuid,
                            COALESCE(_payload->>'program_id', 'personal-development-v3'),
                            _payload->>'element_id',
                            _payload->>'element_type',
                            (_payload->>'area')::SMALLINT,
                            _payload->>'impact_type',
                            (_payload->>'depth')::SMALLINT,
                            (_payload->>'score')::REAL,
                            COALESCE((_payload->>'passed')::BOOLEAN, FALSE),
                            COALESCE(
                                ARRAY(
                                    SELECT elem
                                    FROM jsonb_array_elements_text(
                                        COALESCE(_payload->'errors', '[]'::jsonb)
                                    ) AS elem
                                    WHERE elem IS NOT NULL AND elem != ''
                                ),
                                '{}'::TEXT[]
                            ),
                            (_payload->>'duration_min')::SMALLINT,
                            _payload->'tailor_context',
                            NEW.source,
                            NEW.id,
                            _schema,
                            NEW.created_at
                        );
                    ELSE
                        -- v1: cell-based (6 направлений, Bloom) — legacy
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
                            (_payload->>'direction')::SMALLINT,
                            _payload->>'topic_id',
                            (_payload->>'bloom_level')::SMALLINT,
                            _payload->>'cell_id',
                            (_payload->>'score')::REAL,
                            COALESCE((_payload->>'passed')::BOOLEAN, FALSE),
                            COALESCE(
                                ARRAY(
                                    SELECT elem
                                    FROM jsonb_array_elements_text(
                                        COALESCE(_payload->'errors', '[]'::jsonb)
                                    ) AS elem
                                    WHERE elem IS NOT NULL AND elem != ''
                                ),
                                '{}'::TEXT[]
                            ),
                            (_payload->>'duration_min')::SMALLINT,
                            _payload->'tailor_context',
                            NEW.source,
                            NEW.id,
                            _schema,
                            NEW.created_at
                        );
                    END IF;

                EXCEPTION WHEN OTHERS THEN
                    RAISE WARNING
                        '[learning_history] materialization failed, event_id=%: %',
                        NEW.id, SQLERRM;
                END;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        ''')
        print("  ✅ Trigger function обновлена (v1/v2 dual path)")

        # ═══════════════════════════════════════════════════════════
        # 6. Проверка
        # ═══════════════════════════════════════════════════════════
        count = await conn.fetchval(
            'SELECT COUNT(*) FROM development.learning_history'
        )
        print(f"\n✅ learning_history: {count} записей")
        print("Миграция 010 завершена.")

    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(migrate())
