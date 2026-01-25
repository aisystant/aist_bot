"""
Модели базы данных (SQL схемы).

Содержит CREATE TABLE и миграции.
"""

import asyncpg
from config import get_logger

logger = get_logger(__name__)


async def create_tables(pool: asyncpg.Pool):
    """Создание всех таблиц и применение миграций"""
    async with pool.acquire() as conn:
        # ═══════════════════════════════════════════════════════════
        # ОСНОВНАЯ ТАБЛИЦА ПОЛЬЗОВАТЕЛЕЙ
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS interns (
                chat_id BIGINT PRIMARY KEY,
                
                -- Профиль
                name TEXT DEFAULT '',
                occupation TEXT DEFAULT '',
                role TEXT DEFAULT '',
                domain TEXT DEFAULT '',
                interests TEXT DEFAULT '[]',
                motivation TEXT DEFAULT '',
                goals TEXT DEFAULT '',
                
                -- Предпочтения
                experience_level TEXT DEFAULT '',
                difficulty_preference TEXT DEFAULT '',
                learning_style TEXT DEFAULT '',
                study_duration INTEGER DEFAULT 15,
                schedule_time TEXT DEFAULT '09:00',
                current_problems TEXT DEFAULT '',
                desires TEXT DEFAULT '',
                topic_order TEXT DEFAULT 'default',
                
                -- Режимы (NEW)
                mode TEXT DEFAULT 'marathon',
                current_context TEXT DEFAULT '{}',

                -- State Machine (текущее состояние)
                current_state TEXT DEFAULT NULL,
                
                -- Марафон
                marathon_status TEXT DEFAULT 'not_started',
                marathon_start_date DATE DEFAULT NULL,
                marathon_paused_at DATE DEFAULT NULL,
                current_topic_index INTEGER DEFAULT 0,
                completed_topics TEXT DEFAULT '[]',
                topics_today INTEGER DEFAULT 0,
                last_topic_date DATE DEFAULT NULL,
                
                -- Сложность (бывш. Bloom)
                complexity_level INTEGER DEFAULT 1,
                topics_at_current_complexity INTEGER DEFAULT 0,
                
                -- Лента (NEW)
                feed_status TEXT DEFAULT 'not_started',
                feed_started_at DATE DEFAULT NULL,
                
                -- Систематичность (NEW)
                active_days_total INTEGER DEFAULT 0,
                active_days_streak INTEGER DEFAULT 0,
                longest_streak INTEGER DEFAULT 0,
                last_active_date DATE DEFAULT NULL,
                
                -- Статусы
                onboarding_completed BOOLEAN DEFAULT FALSE,
                
                -- Временные метки
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ═══════════════════════════════════════════════════════════
        # МИГРАЦИИ ДЛЯ СУЩЕСТВУЮЩИХ ТАБЛИЦ
        # ═══════════════════════════════════════════════════════════
        
        # Старые миграции (для совместимости)
        migrations = [
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS study_duration INTEGER DEFAULT 15',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS occupation TEXT DEFAULT \'\'',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS motivation TEXT DEFAULT \'\'',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS topic_order TEXT DEFAULT \'default\'',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS marathon_start_date DATE DEFAULT NULL',
            
            # Переименование bloom -> complexity (с сохранением старых для совместимости)
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS complexity_level INTEGER DEFAULT 1',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS topics_at_current_complexity INTEGER DEFAULT 0',
            
            # Новые поля для режимов
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT \'marathon\'',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS current_context TEXT DEFAULT \'{}\'',

            # State Machine
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS current_state TEXT DEFAULT NULL',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS marathon_status TEXT DEFAULT \'not_started\'',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS marathon_paused_at DATE DEFAULT NULL',
            
            # Лента
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS feed_status TEXT DEFAULT \'not_started\'',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS feed_started_at DATE DEFAULT NULL',
            
            # Систематичность
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS active_days_total INTEGER DEFAULT 0',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS active_days_streak INTEGER DEFAULT 0',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS longest_streak INTEGER DEFAULT 0',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS last_active_date DATE DEFAULT NULL',
        ]
        
        for migration in migrations:
            try:
                await conn.execute(migration)
            except Exception as e:
                # Игнорируем ошибки "колонка уже существует"
                if 'already exists' not in str(e).lower():
                    logger.warning(f"Миграция пропущена: {e}")

        # ═══════════════════════════════════════════════════════════
        # ОТВЕТЫ И РАБОЧИЕ ПРОДУКТЫ
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS answers (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                
                -- Контекст
                mode TEXT DEFAULT 'marathon',
                topic_index INTEGER,
                topic_id TEXT,
                feed_session_id INTEGER,
                
                -- Ответ
                answer_type TEXT DEFAULT 'theory_answer',
                answer TEXT,
                work_product_category TEXT,
                
                -- Метаданные
                complexity_level INTEGER,
                
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        # Миграции для answers
        answer_migrations = [
            'ALTER TABLE answers ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT \'marathon\'',
            'ALTER TABLE answers ADD COLUMN IF NOT EXISTS topic_id TEXT',
            'ALTER TABLE answers ADD COLUMN IF NOT EXISTS feed_session_id INTEGER',
            'ALTER TABLE answers ADD COLUMN IF NOT EXISTS answer_type TEXT DEFAULT \'theory_answer\'',
            'ALTER TABLE answers ADD COLUMN IF NOT EXISTS work_product_category TEXT',
            'ALTER TABLE answers ADD COLUMN IF NOT EXISTS complexity_level INTEGER',
        ]
        
        for migration in answer_migrations:
            try:
                await conn.execute(migration)
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════
        # НАПОМИНАНИЯ
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                reminder_type TEXT,
                scheduled_for TIMESTAMP,
                sent BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ═══════════════════════════════════════════════════════════
        # ЛЕНТА: НЕДЕЛЬНЫЕ ПЛАНЫ (NEW)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS feed_weeks (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,

                week_number INTEGER,
                week_start DATE,

                suggested_topics TEXT DEFAULT '[]',
                accepted_topics TEXT DEFAULT '[]',

                current_day INTEGER DEFAULT 0,
                status TEXT DEFAULT 'planning',

                ended_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Миграции для feed_weeks
        feed_week_migrations = [
            'ALTER TABLE feed_weeks ADD COLUMN IF NOT EXISTS ended_at TIMESTAMP',
        ]
        for migration in feed_week_migrations:
            try:
                await conn.execute(migration)
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════
        # ЛЕНТА: СЕССИИ (NEW)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS feed_sessions (
                id SERIAL PRIMARY KEY,
                week_id INTEGER,

                day_number INTEGER,
                topic_title TEXT,
                content TEXT DEFAULT '{}',

                session_date DATE,
                status TEXT DEFAULT 'active',
                fixation_text TEXT,

                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Миграции для feed_sessions (добавляем недостающие колонки)
        feed_session_migrations = [
            'ALTER TABLE feed_sessions ADD COLUMN IF NOT EXISTS topic_title TEXT',
            'ALTER TABLE feed_sessions ADD COLUMN IF NOT EXISTS session_date DATE',
            'ALTER TABLE feed_sessions ADD COLUMN IF NOT EXISTS status TEXT DEFAULT \'active\'',
            'ALTER TABLE feed_sessions ADD COLUMN IF NOT EXISTS fixation_text TEXT',
        ]
        for migration in feed_session_migrations:
            try:
                await conn.execute(migration)
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════
        # ЛОГ АКТИВНОСТИ (NEW)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                
                activity_date DATE,
                activity_type TEXT,
                mode TEXT,
                reference_id INTEGER,
                
                created_at TIMESTAMP DEFAULT NOW(),
                
                UNIQUE(chat_id, activity_date, activity_type)
            )
        ''')
        
        # Индекс для быстрых запросов
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_activity_date 
            ON activity_log(chat_id, activity_date)
        ''')

        # ═══════════════════════════════════════════════════════════
        # ВОПРОСЫ И ОТВЕТЫ (NEW)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS qa_history (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                
                mode TEXT,
                context_topic TEXT,
                
                question TEXT,
                answer TEXT,
                mcp_sources TEXT DEFAULT '[]',
                
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

    logger.info("✅ Все таблицы созданы/обновлены")
