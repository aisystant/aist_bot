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
                language TEXT DEFAULT 'ru',
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

            # Язык пользователя
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS language TEXT DEFAULT \'ru\'',

            # Второе напоминание
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS schedule_time_2 TEXT DEFAULT NULL',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS feed_schedule_time TEXT DEFAULT NULL',
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
            'ALTER TABLE answers ADD COLUMN IF NOT EXISTS feedback TEXT',
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

        # Дедупликация feed_sessions перед добавлением UNIQUE constraint
        try:
            await conn.execute('''
                WITH keep AS (
                    SELECT DISTINCT ON (week_id, session_date) id
                    FROM feed_sessions
                    WHERE session_date IS NOT NULL
                    ORDER BY week_id, session_date,
                        CASE WHEN status = 'completed' THEN 0
                             WHEN status = 'active' THEN 1
                             ELSE 2 END,
                        created_at DESC
                )
                DELETE FROM feed_sessions
                WHERE session_date IS NOT NULL
                  AND id NOT IN (SELECT id FROM keep)
            ''')
        except Exception:
            pass

        try:
            await conn.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS uq_feed_sessions_week_date
                ON feed_sessions (week_id, session_date)
            ''')
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════
        # МАРАФОН: ПРЕ-ГЕНЕРИРОВАННЫЙ КОНТЕНТ
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS marathon_content (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                topic_index INTEGER NOT NULL,

                lesson_content TEXT,
                question_content TEXT,
                practice_content TEXT,

                bloom_level INTEGER,
                status TEXT DEFAULT 'pending',

                created_at TIMESTAMP DEFAULT NOW(),
                delivered_at TIMESTAMP,

                UNIQUE(chat_id, topic_index)
            )
        ''')

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

        # Индекс для быстрого поиска по chat_id
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_qa_history_chat_id
            ON qa_history(chat_id)
        ''')

        # Миграции qa_history
        qa_migrations = [
            'ALTER TABLE qa_history ADD COLUMN IF NOT EXISTS helpful BOOLEAN',
            'ALTER TABLE qa_history ADD COLUMN IF NOT EXISTS user_comment TEXT',
        ]
        for migration in qa_migrations:
            try:
                await conn.execute(migration)
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════
        # GITHUB ПОДКЛЮЧЕНИЯ (OAuth токены + настройки)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS github_connections (
                chat_id BIGINT PRIMARY KEY,
                access_token TEXT NOT NULL,
                token_type TEXT DEFAULT 'bearer',
                scope TEXT,
                github_username TEXT,
                target_repo TEXT,
                notes_path TEXT DEFAULT 'inbox/fleeting-notes.md',
                strategy_repo TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Миграции для github_connections
        github_migrations = [
            'ALTER TABLE github_connections ADD COLUMN IF NOT EXISTS strategy_repo TEXT',
        ]
        for migration in github_migrations:
            try:
                await conn.execute(migration)
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════
        # ОЦЕНКИ / ТЕСТЫ (assessments)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS assessments (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,

                assessment_id TEXT NOT NULL,
                answers TEXT DEFAULT '{}',
                scores TEXT DEFAULT '{}',
                dominant_state TEXT,
                self_check TEXT,
                open_response TEXT,

                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_assessments_chat_id
            ON assessments(chat_id)
        ''')

        # Миграции для interns — поля последней оценки + сброс статистики
        assessment_migrations = [
            "ALTER TABLE interns ADD COLUMN IF NOT EXISTS assessment_state TEXT DEFAULT NULL",
            "ALTER TABLE interns ADD COLUMN IF NOT EXISTS assessment_date DATE DEFAULT NULL",
            "ALTER TABLE interns ADD COLUMN IF NOT EXISTS stats_reset_date DATE DEFAULT NULL",
        ]
        for migration in assessment_migrations:
            try:
                await conn.execute(migration)
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════
        # ОБРАТНАЯ СВЯЗЬ (feedback_reports)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS feedback_reports (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),

                category TEXT NOT NULL DEFAULT 'bug',
                scenario TEXT DEFAULT 'other',
                severity TEXT NOT NULL DEFAULT 'yellow',

                message TEXT NOT NULL,

                status TEXT DEFAULT 'new',
                notified_at TIMESTAMP DEFAULT NULL
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_feedback_reports_severity_status
            ON feedback_reports(severity, status)
        ''')

        # ═══════════════════════════════════════════════════════════
        # ИСПОЛЬЗОВАНИЕ СЕРВИСОВ (аналитика)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS service_usage (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                service_id TEXT NOT NULL,
                action TEXT DEFAULT 'enter',
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_service_usage_user
            ON service_usage(user_id)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_service_usage_service
            ON service_usage(user_id, service_id)
        ''')

        # ═══════════════════════════════════════════════════════════
        # ПОДПИСКИ (Stars Subscription)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                telegram_payment_charge_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                stars_amount INTEGER NOT NULL,
                started_at TIMESTAMP NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMP NOT NULL,
                cancelled_at TIMESTAMP DEFAULT NULL,
                is_first_recurring BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_subscriptions_chat_id
            ON subscriptions(chat_id)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_subscriptions_active
            ON subscriptions(chat_id, status)
        ''')

        # Миграция interns: trial_started_at
        try:
            await conn.execute(
                'ALTER TABLE interns ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMP DEFAULT NULL'
            )
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════
        # FSM СОСТОЯНИЯ (для aiogram)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS fsm_states (
                chat_id BIGINT PRIMARY KEY,
                state TEXT,
                data TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ═══════════════════════════════════════════════════════════
        # АГРЕГИРОВАННЫЙ ПРОФИЛЬ ЗНАНИЙ (VIEW)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE OR REPLACE VIEW user_knowledge_profile AS
            SELECT
                i.chat_id,
                i.name, i.occupation, i.role, i.domain,
                i.interests, i.goals, i.motivation,
                i.language, i.experience_level,
                -- Learning state
                i.mode, i.marathon_status, i.feed_status,
                i.current_topic_index, i.complexity_level,
                i.assessment_state, i.assessment_date,
                -- Systematicity
                i.active_days_total, i.active_days_streak, i.longest_streak,
                i.last_active_date,
                -- Timestamps
                i.created_at, i.updated_at,
                -- Aggregates: answers
                (SELECT COUNT(*) FROM answers a
                 WHERE a.chat_id = i.chat_id AND a.answer_type = 'theory_answer')
                    AS theory_answers_count,
                (SELECT COUNT(*) FROM answers a
                 WHERE a.chat_id = i.chat_id AND a.answer_type = 'work_product')
                    AS work_products_count,
                -- Aggregates: QA
                (SELECT COUNT(*) FROM qa_history q
                 WHERE q.chat_id = i.chat_id)
                    AS qa_count,
                -- Aggregates: Feed
                (SELECT COUNT(*) FROM feed_sessions fs
                 JOIN feed_weeks fw ON fs.week_id = fw.id
                 WHERE fw.chat_id = i.chat_id)
                    AS total_digests,
                (SELECT COUNT(*) FROM feed_sessions fs
                 JOIN feed_weeks fw ON fs.week_id = fw.id
                 WHERE fw.chat_id = i.chat_id AND fs.status = 'completed')
                    AS total_fixations,
                -- Current feed topics
                (SELECT fw2.accepted_topics FROM feed_weeks fw2
                 WHERE fw2.chat_id = i.chat_id AND fw2.status = 'active'
                 ORDER BY fw2.created_at DESC LIMIT 1)
                    AS current_feed_topics
            FROM interns i
        ''')

        # ═══════════════════════════════════════════════════════════
        # ТРЕЙСИНГ ЗАПРОСОВ (для Grafana)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS request_traces (
                id SERIAL PRIMARY KEY,
                trace_id TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                command TEXT,
                state TEXT,
                total_ms REAL NOT NULL,
                spans JSONB DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_traces_created
            ON request_traces (created_at DESC)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_traces_user
            ON request_traces (user_id, created_at DESC)
        ''')

    logger.info("✅ Все таблицы созданы/обновлены")
