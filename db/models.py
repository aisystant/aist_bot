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

            # Telegram username (@handle)
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS tg_username TEXT DEFAULT NULL',

            # DT connection persistence (DP.D.028)
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS dt_connected_at TIMESTAMP DEFAULT NULL',

            # Bot blocked flag (WP-7: scheduler skip blocked users)
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS bot_blocked BOOLEAN DEFAULT FALSE',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS bot_blocked_at TIMESTAMP DEFAULT NULL',

            # Aisystant account linking (WP-79: единый бот)
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS aisystant_id TEXT DEFAULT NULL',
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS aisystant_linked_at TIMESTAMP DEFAULT NULL',

            # IWE template update notifications (WP-90)
            'ALTER TABLE interns ADD COLUMN IF NOT EXISTS notify_template_updates BOOLEAN DEFAULT FALSE',
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
            'ALTER TABLE github_connections ADD COLUMN IF NOT EXISTS knowledge_repo TEXT',
            "ALTER TABLE github_connections ADD COLUMN IF NOT EXISTS default_branch TEXT DEFAULT 'main'",
            "ALTER TABLE github_connections ADD COLUMN IF NOT EXISTS strategy_default_branch TEXT DEFAULT 'main'",
        ]
        for migration in github_migrations:
            try:
                await conn.execute(migration)
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════
        # WAKATIME ПОДКЛЮЧЕНИЯ (per-user API keys, WP-60)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS wakatime_connections (
                chat_id BIGINT PRIMARY KEY REFERENCES interns(chat_id),
                api_key TEXT NOT NULL,
                wakatime_username TEXT,
                connected_at TIMESTAMP DEFAULT NOW()
            )
        ''')

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
        # АВТО-ТРИАЖ FEEDBACK (feedback_triage)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS feedback_triage (
                id SERIAL PRIMARY KEY,
                qa_id INTEGER NOT NULL REFERENCES qa_history(id),
                chat_id BIGINT NOT NULL,
                question TEXT NOT NULL,
                answer_snippet TEXT,

                -- LLM-классификация
                category TEXT NOT NULL DEFAULT 'unknown',
                severity TEXT NOT NULL DEFAULT 'low',
                cluster TEXT DEFAULT NULL,
                reason TEXT DEFAULT NULL,

                -- Метаданные
                has_comment BOOLEAN DEFAULT FALSE,
                user_comment TEXT DEFAULT NULL,
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT NOW(),
                notified_at TIMESTAMP DEFAULT NULL
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_feedback_triage_severity_status
            ON feedback_triage(severity, status)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_feedback_triage_category
            ON feedback_triage(category)
        ''')

        await conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_triage_qa_id
            ON feedback_triage(qa_id)
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
        # PG не позволяет менять порядок/имена колонок через REPLACE →
        # всегда DROP + CREATE (view stateless, данные не теряются)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('DROP VIEW IF EXISTS user_knowledge_profile')
        await conn.execute('''
            CREATE VIEW user_knowledge_profile AS
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
                -- Timestamps / DT
                i.created_at, i.updated_at, i.dt_connected_at,
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

        # ═══════════════════════════════════════════════════════════
        # МОНИТОРИНГ ОШИБОК
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS error_logs (
                id SERIAL PRIMARY KEY,
                error_key TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'ERROR',
                logger_name TEXT NOT NULL,
                message TEXT NOT NULL,
                traceback TEXT,
                context JSONB DEFAULT '{}',
                occurrence_count INTEGER DEFAULT 1,
                first_seen_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ DEFAULT NOW(),
                alerted BOOLEAN DEFAULT FALSE
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_error_logs_last_seen
            ON error_logs (last_seen_at DESC)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_error_logs_alerted
            ON error_logs (alerted, last_seen_at DESC)
        ''')

        # Classifier columns (WP-45 Phase 2: DP.RUNBOOK.001 classification)
        for col, typedef in [
            ('category', 'TEXT'),
            ('severity', 'TEXT'),
            ('suggested_action', 'TEXT'),
            ('escalated', 'BOOLEAN DEFAULT FALSE'),
        ]:
            await conn.execute(f'''
                ALTER TABLE error_logs ADD COLUMN IF NOT EXISTS {col} {typedef}
            ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_error_logs_category
            ON error_logs (category, last_seen_at DESC)
        ''')

        # ═══════════════════════════════════════════════════════════
        # L2 AUTO-FIX: предложения исправлений с подтверждением (WP-45)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS pending_fixes (
                id SERIAL PRIMARY KEY,
                error_log_id INTEGER NOT NULL,
                error_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                diagnosis TEXT NOT NULL,
                archgate_eval TEXT NOT NULL,
                proposed_diff TEXT NOT NULL,
                file_path TEXT NOT NULL,
                pr_url TEXT,
                branch_name TEXT,
                tg_message_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                resolved_at TIMESTAMPTZ
            )
        ''')

        await conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pf_error_key_active
            ON pending_fixes (error_key) WHERE status IN ('pending', 'approved')
        ''')

        # ═══════════════════════════════════════════════════════════
        # КЕШ КОНТЕНТА (экономия Claude API на повторной генерации)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS content_cache (
                cache_key TEXT PRIMARY KEY,
                content_type TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL
            )
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_content_cache_expires
            ON content_cache (expires_at)
        ''')

        # ═══════════════════════════════════════════════════════════
        # СЕССИИ ПОЛЬЗОВАТЕЛЕЙ (аналитика: длина, частота, entry/exit)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_sessions (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ended_at TIMESTAMPTZ,
                duration_seconds INTEGER,
                request_count INTEGER DEFAULT 1,
                commands JSONB DEFAULT '[]',
                entry_point TEXT,
                exit_point TEXT
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_sessions_chat_id
            ON user_sessions (chat_id)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_sessions_started
            ON user_sessions (started_at DESC)
        ''')

        # ═══════════════════════════════════════════════════════════
        # КОНВЕРСИОННЫЕ СОБЫТИЯ (DP.ARCH.002 § 12.8)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS conversion_events (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                trigger_type TEXT NOT NULL,
                milestone TEXT,
                shown_at TIMESTAMPTZ DEFAULT NOW(),
                action TEXT DEFAULT 'shown'
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversion_chat_id
            ON conversion_events (chat_id)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversion_trigger
            ON conversion_events (trigger_type, milestone)
        ''')

        # ═══════════════════════════════════════════════════════════
        # DISCOURSE: АККАУНТЫ И ПУБЛИКАЦИИ (WP-53)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS discourse_accounts (
                chat_id BIGINT PRIMARY KEY REFERENCES interns(chat_id),
                discourse_username TEXT NOT NULL,
                blog_category_id INTEGER,
                blog_category_slug TEXT,
                connected_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS published_posts (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                discourse_topic_id INTEGER NOT NULL,
                discourse_post_id INTEGER,
                title TEXT NOT NULL,
                source_file TEXT,
                category_id INTEGER,
                posts_count INTEGER DEFAULT 1,
                last_checked_at TIMESTAMP DEFAULT NOW(),
                published_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_published_posts_topic
            ON published_posts (discourse_topic_id)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_published_posts_chat
            ON published_posts (chat_id)
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_publications (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                raw TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                tags TEXT DEFAULT '[]',
                schedule_time TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'pending',
                discourse_topic_id INTEGER,
                source_file TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_scheduled_pubs_pending
            ON scheduled_publications (status, schedule_time)
            WHERE status = 'pending'
        ''')

        # Migration: source_file for smart publisher (WP-53 Phase 3)
        try:
            await conn.execute(
                'ALTER TABLE scheduled_publications ADD COLUMN IF NOT EXISTS source_file TEXT'
            )
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════
        # TIER EVENTS: аналитика переходов между тирами (WP-52)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tier_events (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                from_tier INTEGER NOT NULL,
                to_tier INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_tier_events_chat
            ON tier_events (chat_id, created_at DESC)
        ''')

        # Migration: comment_check_failures for published_posts (WP-7: skip deleted topics)
        try:
            await conn.execute(
                'ALTER TABLE published_posts ADD COLUMN IF NOT EXISTS comment_check_failures INTEGER DEFAULT 0'
            )
        except Exception:
            pass

        # ============= ТРЕНИРОВКА (WP-55) =============

        # Настройки тренировки (когнитивный уровень + включённые принципы)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS training_settings (
                chat_id BIGINT PRIMARY KEY,
                cognitive_level TEXT DEFAULT 'postformal',
                enabled_principles TEXT DEFAULT '["ZP.1","ZP.2","ZP.3","ZP.4","ZP.5","ZP.6"]',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Прогресс по принципам (текущая глубина каждого принципа)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS training_progress (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                principle_id TEXT NOT NULL,
                current_depth INTEGER DEFAULT 0,
                attempts_at_depth INTEGER DEFAULT 0,
                last_completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(chat_id, principle_id)
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_training_progress_chat
            ON training_progress (chat_id)
        ''')

        # Попытки (история ответов на задания)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS training_attempts (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                principle_id TEXT NOT NULL,
                depth INTEGER NOT NULL,
                assignment_text TEXT,
                answer_text TEXT,
                passed BOOLEAN DEFAULT FALSE,
                feedback TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_training_attempts_chat
            ON training_attempts (chat_id, principle_id)
        ''')

        # Миграция: training_mode + single_principle (WP-55 v2)
        try:
            await conn.execute('''
                ALTER TABLE training_settings
                ADD COLUMN IF NOT EXISTS training_mode TEXT DEFAULT 'shuffle'
            ''')
            await conn.execute('''
                ALTER TABLE training_settings
                ADD COLUMN IF NOT EXISTS single_principle TEXT
            ''')
        except Exception:
            pass  # Колонки уже существуют

        # ============= ТРЕНИРОВКА РЕБЁНКА (WP-55 Phase 2) =============

        # Профили детей (дочерние ЦД)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS training_children (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                cognitive_level TEXT NOT NULL DEFAULT 'concrete_operational',
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_training_children_chat
            ON training_children (chat_id)
        ''')

        # Миграция: child_id в training_progress и training_attempts
        try:
            await conn.execute('''
                ALTER TABLE training_progress
                ADD COLUMN IF NOT EXISTS child_id INTEGER DEFAULT NULL
            ''')
            await conn.execute('''
                ALTER TABLE training_attempts
                ADD COLUMN IF NOT EXISTS child_id INTEGER DEFAULT NULL
            ''')
        except Exception:
            pass

        # Обновить UNIQUE constraint для training_progress (chat_id, principle_id, child_id)
        # Старый: UNIQUE(chat_id, principle_id) — оставляем для NULL child_id (взрослый)
        # Новый индекс для child_id != NULL
        try:
            await conn.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_training_progress_child
                ON training_progress (chat_id, principle_id, child_id)
                WHERE child_id IS NOT NULL
            ''')
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════
        # ЦИФРОВОЙ ДВОЙНИК: schema development + user_events (WP-85)
        # Append-only event log — основа 3-слойной архитектуры ЦД
        # (DP.ARCH.003: Events → State → Views)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('CREATE SCHEMA IF NOT EXISTS development')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS development.user_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'bot',
                payload JSONB DEFAULT '{}',
                confidence REAL DEFAULT 1.0,
                skill_ids TEXT[] DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_events_user_id
            ON development.user_events (user_id, created_at DESC)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_events_type
            ON development.user_events (event_type, created_at DESC)
        ''')

        # Миграция user_events: user_uuid (WP-82 Phase 2) — ДО создания view
        try:
            await conn.execute(
                'ALTER TABLE development.user_events '
                'ADD COLUMN IF NOT EXISTS user_uuid UUID'
            )
        except Exception:
            pass

        # ─── Layer 2: Engagement View (WP-85, подзадача 7) ───
        # DROP + CREATE (§10.22: REPLACE запрещён)
        await conn.execute('DROP VIEW IF EXISTS development.engagement')
        await conn.execute('''
            CREATE VIEW development.engagement AS
            SELECT
                user_id,
                user_uuid,
                COUNT(*) FILTER (WHERE event_type = 'session_start') AS sessions_total,
                COUNT(*) FILTER (WHERE event_type = 'ai_chat') AS ai_chats_total,
                COUNT(*) FILTER (WHERE event_type = 'marathon_step') AS marathon_steps_total,
                COUNT(*) FILTER (WHERE event_type = 'marathon_task') AS marathon_tasks_total,
                COUNT(*) FILTER (WHERE event_type = 'feed_completed') AS feed_completed_total,
                COUNT(*) FILTER (WHERE event_type = 'training_attempt') AS training_attempts_total,
                COUNT(*) FILTER (WHERE event_type = 'training_attempt' AND (payload->>$$passed$$)::boolean = true) AS training_passed_total,
                COUNT(*) FILTER (WHERE event_type = 'assessment_completed') AS assessments_total,
                COUNT(*) AS events_total,
                MIN(created_at) AS first_event_at,
                MAX(created_at) AS last_event_at,
                COUNT(DISTINCT created_at::date) AS active_days,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days') AS events_last_7d,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '30 days') AS events_last_30d
            FROM development.user_events
            GROUP BY user_id, user_uuid
        ''')

        # ═══════════════════════════════════════════════════════════
        # ТОКЕНЫ ЦИФРОВОГО ДВОЙНИКА (WP-82: token persistence)
        # Хранит OAuth токены DT MCP, чтобы подключение не терялось
        # при редеплое бота
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS dt_tokens (
                chat_id BIGINT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                dt_user_id TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ═══════════════════════════════════════════════════════════
        # ЕДИНАЯ ТАБЛИЦА ИДЕНТИЧНОСТИ (WP-82 Phase 2)
        # Identity layer: telegram_id → ory_id → dt_user_id
        # Все бот-таблицы — часть ЦД. T0 без Ory, T1+ с ory_id.
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS public.users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                ory_id UUID UNIQUE,
                telegram_id BIGINT UNIQUE NOT NULL,
                dt_user_id TEXT UNIQUE,
                email TEXT,
                name TEXT,
                language TEXT DEFAULT 'ru',
                timezone TEXT DEFAULT 'Europe/Moscow',
                tier TEXT DEFAULT 'T0',
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                updated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
        ''')

        # Миграция: interns.user_id → FK на users.id
        try:
            await conn.execute(
                'ALTER TABLE interns ADD COLUMN IF NOT EXISTS user_id UUID'
            )
        except Exception:
            pass

        # Backfill: создать записи в users из interns (если ещё нет)
        try:
            inserted = await conn.execute('''
                INSERT INTO public.users (telegram_id, dt_user_id, name, language)
                SELECT i.chat_id, i.dt_user_id, i.name, i.language
                FROM interns i
                WHERE NOT EXISTS (
                    SELECT 1 FROM public.users u WHERE u.telegram_id = i.chat_id
                )
            ''')
            if inserted and inserted != 'INSERT 0':
                logger.info(f"[Migration] Backfill users from interns: {inserted}")
        except Exception as e:
            logger.warning(f"[Migration] Backfill users skipped: {e}")

        # Backfill: записать user_id в interns из users
        try:
            updated = await conn.execute('''
                UPDATE interns SET user_id = u.id
                FROM public.users u
                WHERE interns.chat_id = u.telegram_id
                  AND interns.user_id IS NULL
            ''')
            if updated and updated != 'UPDATE 0':
                logger.info(f"[Migration] Backfill interns.user_id: {updated}")
        except Exception as e:
            logger.warning(f"[Migration] Backfill interns.user_id skipped: {e}")

        # Backfill: user_events.user_uuid из users по telegram_id
        try:
            updated = await conn.execute('''
                UPDATE development.user_events e
                SET user_uuid = u.id
                FROM public.users u
                WHERE e.user_id = u.telegram_id
                  AND e.user_uuid IS NULL
            ''')
            if updated and updated != 'UPDATE 0':
                logger.info(f"[Migration] Backfill user_events.user_uuid: {updated}")
        except Exception as e:
            logger.warning(f"[Migration] Backfill user_events.user_uuid skipped: {e}")

    logger.info("✅ Все таблицы созданы/обновлены")
