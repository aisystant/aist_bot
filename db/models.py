"""
Модели базы данных (SQL схемы).

Содержит CREATE TABLE и миграции.
ЦД-native архитектура (WP-82 Phase 3):
  public.users — identity + profile
  development.user_state — bot state (replaces interns)
"""

import os

import asyncpg
from config import get_logger

logger = get_logger(__name__)

SKIP_DB_MIGRATIONS = os.getenv('SKIP_DB_MIGRATIONS', '').lower() in ('true', '1', 'yes')


async def create_tables(pool: asyncpg.Pool):
    """Создание всех таблиц и применение миграций"""
    if SKIP_DB_MIGRATIONS:
        logger.info("DB migrations skipped (SKIP_DB_MIGRATIONS=true)")
        return
    async with pool.acquire() as conn:
        # ═══════════════════════════════════════════════════════════
        # DEVELOPMENT SCHEMA
        # ═══════════════════════════════════════════════════════════
        await conn.execute('CREATE SCHEMA IF NOT EXISTS development')

        # ═══════════════════════════════════════════════════════════
        # ЕДИНАЯ ТАБЛИЦА ИДЕНТИЧНОСТИ + ПРОФИЛЬ (WP-82 Phase 3)
        # Identity + Profile: telegram_id → ory_id → dt_user_id
        # T0 без Ory, T1+ с ory_id.
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS public.users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                ory_id UUID UNIQUE,
                telegram_id BIGINT UNIQUE NOT NULL,
                dt_user_id TEXT UNIQUE,
                email TEXT,

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
                timezone TEXT DEFAULT 'Europe/Moscow',
                experience_level TEXT DEFAULT '',
                difficulty_preference TEXT DEFAULT '',
                learning_style TEXT DEFAULT '',
                study_duration INTEGER DEFAULT 15,
                current_problems TEXT DEFAULT '',
                desires TEXT DEFAULT '',

                -- Интеграции
                tg_username TEXT,
                aisystant_id TEXT,
                aisystant_linked_at TIMESTAMP,
                dt_connected_at TIMESTAMP,

                -- Статус
                tier TEXT DEFAULT 'T0',

                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                updated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
        ''')

        # Миграции для users (добавление профильных полей — Phase 3)
        user_profile_migrations = [
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS occupation TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS domain TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS interests TEXT DEFAULT '[]'",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS motivation TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS goals TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS experience_level TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS difficulty_preference TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS learning_style TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS study_duration INTEGER DEFAULT 15",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS current_problems TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS desires TEXT DEFAULT ''",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS tg_username TEXT",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS aisystant_id TEXT",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS aisystant_linked_at TIMESTAMP",
            "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS dt_connected_at TIMESTAMP",
        ]
        for migration in user_profile_migrations:
            try:
                await conn.execute(migration)
            except Exception as e:
                if 'already exists' not in str(e).lower():
                    logger.warning(f"User migration skipped: {e}")

        # ═══════════════════════════════════════════════════════════
        # BOT STATE (replaces interns, WP-82 Phase 3)
        # Состояние бота: марафон, лента, SM, расписание, стрики
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS development.user_state (
                user_id UUID PRIMARY KEY REFERENCES public.users(id),
                chat_id BIGINT UNIQUE NOT NULL,

                -- Режимы
                mode TEXT DEFAULT 'marathon',
                current_context TEXT DEFAULT '{}',
                current_state TEXT DEFAULT NULL,
                topic_order TEXT DEFAULT 'default',

                -- Расписание
                schedule_time TEXT DEFAULT '09:00',
                schedule_time_2 TEXT DEFAULT NULL,
                feed_schedule_time TEXT DEFAULT NULL,

                -- Марафон
                marathon_status TEXT DEFAULT 'not_started',
                marathon_start_date DATE DEFAULT NULL,
                marathon_paused_at DATE DEFAULT NULL,
                current_topic_index INTEGER DEFAULT 0,
                completed_topics TEXT DEFAULT '[]',
                topics_today INTEGER DEFAULT 0,
                last_topic_date DATE DEFAULT NULL,

                -- Сложность
                complexity_level INTEGER DEFAULT 1,
                topics_at_current_complexity INTEGER DEFAULT 0,

                -- Лента
                feed_status TEXT DEFAULT 'not_started',
                feed_started_at DATE DEFAULT NULL,

                -- Систематичность
                active_days_total INTEGER DEFAULT 0,
                active_days_streak INTEGER DEFAULT 0,
                longest_streak INTEGER DEFAULT 0,
                last_active_date DATE DEFAULT NULL,

                -- Статусы
                onboarding_completed BOOLEAN DEFAULT FALSE,
                bot_blocked BOOLEAN DEFAULT FALSE,
                bot_blocked_at TIMESTAMP DEFAULT NULL,
                bot_recheck_at TIMESTAMP DEFAULT NULL,
                trial_started_at TIMESTAMP DEFAULT NULL,

                -- Онбордер (WP-406 Ф5): отметки закрытия характеристик Первокурсника
                x2_completed_at TIMESTAMP DEFAULT NULL,
                x3_completed_at TIMESTAMP DEFAULT NULL,

                -- Оценка
                assessment_state TEXT DEFAULT NULL,
                assessment_date DATE DEFAULT NULL,
                stats_reset_date DATE DEFAULT NULL,

                -- Уведомления
                notify_template_updates BOOLEAN DEFAULT FALSE,
                notify_nudges BOOLEAN DEFAULT TRUE,

                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                updated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
        ''')

        # Migration: add notify_nudges column if missing
        await conn.execute('''
            ALTER TABLE development.user_state
            ADD COLUMN IF NOT EXISTS notify_nudges BOOLEAN DEFAULT TRUE
        ''')

        # Migration 030: отметки завершения характеристик Онбордера (WP-406 Ф5)
        await conn.execute('''
            ALTER TABLE development.user_state
            ADD COLUMN IF NOT EXISTS x2_completed_at TIMESTAMP DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS x3_completed_at TIMESTAMP DEFAULT NULL
        ''')

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
        # ДОСТАВЩИК: приватная очередь исходящих (WP-418 Ф3)
        # see DP.SC.177, DP.ROLE.075. Снаружи модуля core.notification_service —
        # raw SQL запрещён. Схема pgqueuer-ready (priority/dedup_key/scheduled_at/
        # locked_at/attempts) — LISTEN/NOTIFY встанет потом без переписывания.
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS notification_queue (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                notification_class TEXT NOT NULL,
                payload JSONB NOT NULL,
                priority INTEGER NOT NULL,
                dedup_key TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                reason TEXT,
                scheduled_at TIMESTAMP DEFAULT NOW(),
                locked_at TIMESTAMP,
                attempts INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                sent_at TIMESTAMP
            )
        ''')
        # Потолок класса: COUNT WHERE chat_id, class, day, status IN (queued, sent).
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_notif_queue_cap
            ON notification_queue(chat_id, notification_class, created_at)
        ''')
        # Дренаж: queued по приоритету.
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_notif_queue_drain
            ON notification_queue(status, priority, scheduled_at)
        ''')
        # Дедуп по ключу.
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_notif_queue_dedup
            ON notification_queue(dedup_key)
        ''')

        # ═══════════════════════════════════════════════════════════
        # ЛЕНТА: НЕДЕЛЬНЫЕ ПЛАНЫ
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

        feed_week_migrations = [
            'ALTER TABLE feed_weeks ADD COLUMN IF NOT EXISTS ended_at TIMESTAMP',
        ]
        for migration in feed_week_migrations:
            try:
                await conn.execute(migration)
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════
        # ЛЕНТА: СЕССИИ
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

        # Индекс для delete_feed_sessions (week_id + status) — устраняет seq scan
        # при смене тем в Ленте (peer-session WP-396, latency 41s → <1s).
        try:
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_feed_sessions_week_status
                ON feed_sessions (week_id, status)
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
                notification_sent_at TIMESTAMP,

                UNIQUE(chat_id, topic_index)
            )
        ''')

        # Миграция: добавить notification_sent_at если отсутствует
        try:
            await conn.execute('''
                ALTER TABLE marathon_content
                ADD COLUMN IF NOT EXISTS notification_sent_at TIMESTAMP
            ''')
        except Exception:
            pass

        # Миграция: добавить fail_count в reminders для retry limit
        try:
            await conn.execute('''
                ALTER TABLE reminders
                ADD COLUMN IF NOT EXISTS fail_count INTEGER DEFAULT 0
            ''')
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════
        # УНИВЕРСАЛЬНЫЙ ЛОГ УВЕДОМЛЕНИЙ (WP-152)
        # Единая таблица idempotent notifications, заменяет:
        # reminders.sent, notification_sent_at
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS notification_log (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                notification_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload JSONB DEFAULT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(idempotency_key)
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_notification_log_chat_type
            ON notification_log(chat_id, notification_type)
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_notification_log_created
            ON notification_log(created_at)
        ''')

        # ═══════════════════════════════════════════════════════════
        # ЛОГ АКТИВНОСТИ
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

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_activity_date
            ON activity_log(chat_id, activity_date)
        ''')

        # ═══════════════════════════════════════════════════════════
        # ВОПРОСЫ И ОТВЕТЫ
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

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_qa_history_chat_id
            ON qa_history(chat_id)
        ''')

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
        # OAUTH PENDING STATES (переживают редеплой, WP-212)
        # Единое хранилище для всех OAuth провайдеров (GitHub, Linear, Ory)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS oauth_pending_states (
                state TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                telegram_user_id BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_oauth_states_created
            ON oauth_pending_states(created_at)
        ''')

        # ═══════════════════════════════════════════════════════════
        # GOOGLE CALENDAR ПОДКЛЮЧЕНИЯ (OAuth tokens, WP-128)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS google_calendar_connections (
                chat_id BIGINT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TIMESTAMP,
                email TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ═══════════════════════════════════════════════════════════
        # WAKATIME: таблица wakatime_connections удалена (WP-109/WP-7).
        # Все данные хранятся в development.user_integrations (service='wakatime').
        # ═══════════════════════════════════════════════════════════

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

        # Migration 008 fallback: feedback_unified lifecycle fields
        for col, typedef in [
            ('source', "TEXT NOT NULL DEFAULT 'bot'"),
            ('suggested_action', 'TEXT DEFAULT NULL'),
            ('confidence', 'REAL DEFAULT NULL'),
            ('resolved_at', 'TIMESTAMP DEFAULT NULL'),
            ('resolution_note', 'TEXT DEFAULT NULL'),
        ]:
            await conn.execute(f'''
                ALTER TABLE feedback_triage ADD COLUMN IF NOT EXISTS {col} {typedef}
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
        # WP-82 Phase 3: JOIN users + user_state
        # ═══════════════════════════════════════════════════════════
        await conn.execute('DROP VIEW IF EXISTS user_knowledge_profile')
        await conn.execute('''
            CREATE VIEW user_knowledge_profile AS
            SELECT
                s.chat_id,
                u.name, u.occupation, u.role, u.domain,
                u.interests, u.goals, u.motivation,
                u.language, u.experience_level,
                -- Learning state
                s.mode, s.marathon_status, s.feed_status,
                s.current_topic_index, s.complexity_level,
                s.assessment_state, s.assessment_date,
                -- Systematicity
                s.active_days_total, s.active_days_streak, s.longest_streak,
                s.last_active_date,
                -- Timestamps / DT
                u.created_at, u.updated_at, u.dt_connected_at, u.dt_user_id,
                -- Aggregates: answers — мигрированы в learning BD (WP-268 Phase 5 G5)
                0::bigint AS theory_answers_count,
                0::bigint AS work_products_count,
                -- Aggregates: QA — мигрированы в journal BD (WP-268 Phase 3 Block 2)
                0::bigint AS qa_count,
                -- Aggregates: Feed
                (SELECT COUNT(*) FROM feed_sessions fs
                 JOIN feed_weeks fw ON fs.week_id = fw.id
                 WHERE fw.chat_id = s.chat_id)
                    AS total_digests,
                (SELECT COUNT(*) FROM feed_sessions fs
                 JOIN feed_weeks fw ON fs.week_id = fw.id
                 WHERE fw.chat_id = s.chat_id AND fs.status = 'completed')
                    AS total_fixations,
                -- Current feed topics
                (SELECT fw2.accepted_topics FROM feed_weeks fw2
                 WHERE fw2.chat_id = s.chat_id AND fw2.status = 'active'
                 ORDER BY fw2.created_at DESC LIMIT 1)
                    AS current_feed_topics
            FROM development.user_state s
            JOIN public.users u ON u.id = s.user_id
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

        # Classifier columns (WP-45 Phase 2)
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

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_error_logs_escalated
            ON error_logs (escalated, last_seen_at DESC)
        ''')

        # ═══════════════════════════════════════════════════════════
        # L2 AUTO-FIX (WP-45)
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

        # content_cache → learning pool (WP-253, cache.py использует get_learning_pool)

        # ═══════════════════════════════════════════════════════════
        # СЕССИИ ПОЛЬЗОВАТЕЛЕЙ
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
        # FK removed (WP-82 Phase 3: interns dropped)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS discourse_accounts (
                chat_id BIGINT PRIMARY KEY,
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

        try:
            await conn.execute(
                'ALTER TABLE scheduled_publications ADD COLUMN IF NOT EXISTS source_file TEXT'
            )
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════
        # TIER EVENTS (WP-52)
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

        try:
            await conn.execute(
                'ALTER TABLE published_posts ADD COLUMN IF NOT EXISTS comment_check_failures INTEGER DEFAULT 0'
            )
        except Exception:
            pass

        # ============= ТРЕНИРОВКА (WP-55) =============

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS training_settings (
                chat_id BIGINT PRIMARY KEY,
                cognitive_level TEXT DEFAULT 'postformal',
                enabled_principles TEXT DEFAULT '["ZP.1","ZP.2","ZP.3","ZP.4","ZP.5","ZP.6"]',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

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
            pass

        # ============= ТРЕНИРОВКА РЕБЁНКА (WP-55 Phase 2) =============

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

        try:
            await conn.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_training_progress_child
                ON training_progress (chat_id, principle_id, child_id)
                WHERE child_id IS NOT NULL
            ''')
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════
        # ЦИФРОВОЙ ДВОЙНИК: user_events (WP-85)
        # ═══════════════════════════════════════════════════════════

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

        try:
            await conn.execute(
                'ALTER TABLE development.user_events '
                'ADD COLUMN IF NOT EXISTS user_uuid UUID'
            )
        except Exception:
            pass

        # ─── Layer 2: Engagement View ───
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
                -- WP-151 Ф3: новые операционные метрики
                COUNT(*) FILTER (WHERE event_type = 'onboarding_completed') AS onboarding_completed_total,
                COUNT(*) FILTER (WHERE event_type = 'mode_changed') AS mode_changes_total,
                COUNT(*) FILTER (WHERE event_type = 'settings_changed') AS settings_changes_total,
                COUNT(*) FILTER (WHERE event_type = 'reminder_delivered') AS reminders_delivered_total,
                COUNT(*) FILTER (WHERE event_type = 'reminder_opened') AS reminders_opened_total,
                COUNT(*) FILTER (WHERE event_type = 'error_shown') AS errors_shown_total,
                COUNT(*) FILTER (WHERE event_type = 'help_viewed') AS help_views_total,
                COUNT(*) FILTER (WHERE event_type = 'progress_viewed') AS progress_views_total,
                COUNT(*) FILTER (WHERE event_type = 'marathon_completed') AS marathon_completions_total,
                COUNT(*) AS events_total,
                MIN(created_at) AS first_event_at,
                MAX(created_at) AS last_event_at,
                COUNT(DISTINCT created_at::date) AS active_days,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days') AS events_last_7d,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '30 days') AS events_last_30d
            FROM development.user_events
            GROUP BY user_id, user_uuid
        ''')

        # ─── Layer 2b: Notification Engagement View (WP-152 Ф4) ───
        await conn.execute(
            'DROP VIEW IF EXISTS development.notification_engagement'
        )
        await conn.execute('''
            CREATE VIEW development.notification_engagement AS
            SELECT
                u.telegram_id AS user_id,
                u.ory_id AS user_uuid,
                COUNT(*) AS notifications_total,
                COUNT(*) FILTER (
                    WHERE nl.created_at > NOW() - INTERVAL '7 days'
                ) AS notifications_7d,
                COUNT(*) FILTER (
                    WHERE nl.created_at > NOW() - INTERVAL '30 days'
                ) AS notifications_30d,
                COUNT(DISTINCT nl.notification_type) AS notification_types,
                COUNT(*) FILTER (
                    WHERE nl.notification_type = 'marathon_lesson'
                ) AS lesson_notifications,
                COUNT(*) FILTER (
                    WHERE nl.notification_type = 'reminder'
                ) AS reminder_notifications,
                COUNT(*) FILTER (
                    WHERE nl.notification_type = 'nudge'
                ) AS nudge_notifications,
                COUNT(*) FILTER (
                    WHERE nl.notification_type = 'trial_expiry'
                ) AS trial_expiry_notifications,
                COUNT(*) FILTER (
                    WHERE nl.notification_type = 'feed_digest'
                ) AS feed_digest_notifications,
                COUNT(*) FILTER (
                    WHERE nl.notification_type = 'milestone'
                ) AS milestone_notifications,
                MIN(nl.created_at) AS first_notification_at,
                MAX(nl.created_at) AS last_notification_at
            FROM notification_log nl
            JOIN public.users u ON u.telegram_id = nl.chat_id
            GROUP BY u.telegram_id, u.ory_id
        ''')

        # ═══════════════════════════════════════════════════════════
        # ТОКЕНЫ ЦИФРОВОГО ДВОЙНИКА (WP-82)
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
        # ТОКЕНЫ ORY OAUTH (WP-209: бот→Gateway)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS ory_tokens (
                chat_id BIGINT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                ory_id TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ═══════════════════════════════════════════════════════════
        # МИГРАЦИЯ: interns → users + user_state (WP-82 Phase 3)
        # Одноразовая миграция при первом запуске после обновления.
        # ═══════════════════════════════════════════════════════════
        has_interns = await conn.fetchval('''
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'interns' AND table_schema = 'public'
            )
        ''')

        if has_interns:
            logger.info("[Migration] Found interns table — migrating to users + user_state...")

            # Step 1: Backfill users with profile data from interns
            try:
                inserted = await conn.execute('''
                    INSERT INTO public.users (
                        telegram_id, name, occupation, role, domain,
                        interests, motivation, goals, language, experience_level,
                        difficulty_preference, learning_style, study_duration,
                        current_problems, desires, tg_username,
                        aisystant_id, aisystant_linked_at, dt_connected_at, created_at
                    )
                    SELECT
                        i.chat_id, i.name, i.occupation, i.role, i.domain,
                        i.interests, i.motivation, i.goals, i.language, i.experience_level,
                        i.difficulty_preference, i.learning_style, i.study_duration,
                        i.current_problems, i.desires, i.tg_username,
                        i.aisystant_id, i.aisystant_linked_at, i.dt_connected_at, i.created_at
                    FROM interns i
                    WHERE NOT EXISTS (
                        SELECT 1 FROM public.users u WHERE u.telegram_id = i.chat_id
                    )
                ''')
                if inserted and inserted != 'INSERT 0':
                    logger.info(f"[Migration] Users backfill: {inserted}")

                # Update existing users with profile data from interns
                await conn.execute('''
                    UPDATE public.users u SET
                        name = COALESCE(NULLIF(i.name, ''), u.name),
                        occupation = COALESCE(NULLIF(i.occupation, ''), u.occupation),
                        role = COALESCE(NULLIF(i.role, ''), u.role),
                        domain = COALESCE(NULLIF(i.domain, ''), u.domain),
                        interests = CASE WHEN i.interests != '[]' THEN i.interests ELSE u.interests END,
                        motivation = COALESCE(NULLIF(i.motivation, ''), u.motivation),
                        goals = COALESCE(NULLIF(i.goals, ''), u.goals),
                        language = COALESCE(NULLIF(i.language, ''), u.language),
                        experience_level = COALESCE(NULLIF(i.experience_level, ''), u.experience_level),
                        difficulty_preference = COALESCE(NULLIF(i.difficulty_preference, ''), u.difficulty_preference),
                        learning_style = COALESCE(NULLIF(i.learning_style, ''), u.learning_style),
                        study_duration = COALESCE(i.study_duration, u.study_duration),
                        current_problems = COALESCE(NULLIF(i.current_problems, ''), u.current_problems),
                        desires = COALESCE(NULLIF(i.desires, ''), u.desires),
                        tg_username = COALESCE(i.tg_username, u.tg_username),
                        aisystant_id = COALESCE(i.aisystant_id, u.aisystant_id),
                        aisystant_linked_at = COALESCE(i.aisystant_linked_at, u.aisystant_linked_at),
                        dt_connected_at = COALESCE(i.dt_connected_at, u.dt_connected_at)
                    FROM interns i
                    WHERE u.telegram_id = i.chat_id
                ''')
            except Exception as e:
                logger.warning(f"[Migration] Users profile backfill error: {e}")

            # Step 2: Backfill user_state from interns
            try:
                inserted = await conn.execute('''
                    INSERT INTO development.user_state (
                        user_id, chat_id, mode, current_context, current_state, topic_order,
                        schedule_time, schedule_time_2, feed_schedule_time,
                        marathon_status, marathon_start_date, marathon_paused_at,
                        current_topic_index, completed_topics, topics_today, last_topic_date,
                        complexity_level, topics_at_current_complexity,
                        feed_status, feed_started_at,
                        active_days_total, active_days_streak, longest_streak, last_active_date,
                        onboarding_completed, bot_blocked, bot_blocked_at, bot_recheck_at, trial_started_at,
                        assessment_state, assessment_date, stats_reset_date,
                        notify_template_updates, created_at
                    )
                    SELECT
                        u.id, i.chat_id, i.mode, i.current_context, i.current_state, i.topic_order,
                        i.schedule_time, i.schedule_time_2, i.feed_schedule_time,
                        i.marathon_status, i.marathon_start_date, i.marathon_paused_at,
                        i.current_topic_index, i.completed_topics, i.topics_today, i.last_topic_date,
                        i.complexity_level, i.topics_at_current_complexity,
                        i.feed_status, i.feed_started_at,
                        i.active_days_total, i.active_days_streak, i.longest_streak, i.last_active_date,
                        i.onboarding_completed, i.bot_blocked, i.bot_blocked_at, i.bot_recheck_at, i.trial_started_at,
                        i.assessment_state, i.assessment_date, i.stats_reset_date,
                        i.notify_template_updates, i.created_at
                    FROM interns i
                    JOIN public.users u ON u.telegram_id = i.chat_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM development.user_state s WHERE s.chat_id = i.chat_id
                    )
                ''')
                if inserted and inserted != 'INSERT 0':
                    logger.info(f"[Migration] user_state backfill: {inserted}")
            except Exception as e:
                logger.warning(f"[Migration] user_state backfill error: {e}")

            # Step 3: Backfill user_events.user_uuid
            try:
                updated = await conn.execute('''
                    UPDATE development.user_events e
                    SET user_uuid = u.id
                    FROM public.users u
                    WHERE e.user_id = u.telegram_id
                      AND e.user_uuid IS NULL
                ''')
                if updated and updated != 'UPDATE 0':
                    logger.info(f"[Migration] user_events.user_uuid backfill: {updated}")
            except Exception as e:
                logger.warning(f"[Migration] user_events.user_uuid backfill skipped: {e}")

            # Step 4: Drop interns (CASCADE removes FK from wakatime/discourse)
            try:
                await conn.execute('DROP TABLE IF EXISTS interns CASCADE')
                logger.info("[Migration] Dropped interns table")
            except Exception as e:
                logger.warning(f"[Migration] Failed to drop interns: {e}")

        # Backfill: dt_user_id из dt_tokens → users (WP-82)
        try:
            updated = await conn.execute('''
                UPDATE public.users SET dt_user_id = dt_tokens.dt_user_id
                FROM dt_tokens
                WHERE public.users.telegram_id = dt_tokens.chat_id
                  AND dt_tokens.dt_user_id IS NOT NULL
                  AND public.users.dt_user_id IS NULL
            ''')
            if updated and updated != 'UPDATE 0':
                logger.info(f"[Migration] Backfill dt_user_id: {updated}")
        except Exception as e:
            logger.warning(f"[Migration] Backfill dt_user_id skipped: {e}")

        # ═══════════════════════════════════════════════════════════
        # МОНИТОРИНГ КАНАЛОВ (SC.118)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS channel_monitors (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                channel_title TEXT,
                user_id UUID NOT NULL REFERENCES public.users(id),
                chat_id BIGINT NOT NULL,

                -- Настройки мониторинга
                is_admin BOOLEAN DEFAULT FALSE,
                track_username BOOLEAN DEFAULT TRUE,
                track_reply BOOLEAN DEFAULT TRUE,
                track_name BOOLEAN DEFAULT TRUE,
                active BOOLEAN DEFAULT TRUE,

                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                updated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),

                UNIQUE(channel_id, chat_id)
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS channel_mentions_log (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                mentioned_chat_id BIGINT NOT NULL,

                mention_type TEXT NOT NULL,
                draft_sent BOOLEAN DEFAULT FALSE,

                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),

                UNIQUE(channel_id, message_id, mentioned_chat_id)
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_channel_monitors_channel
            ON channel_monitors(channel_id) WHERE active = TRUE
        ''')

        # ═══════════════════════════════════════════════════════════
        # СООБЩЕСТВО IWE: ОПЛАТЫ + УЧАСТНИКИ (WP-181)
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS workshop_payments (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                aisystant_id TEXT,
                amount NUMERIC NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT NOT NULL DEFAULT 'bot',
                payment_id TEXT,
                paid_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_workshop_payments_tg
            ON workshop_payments (telegram_id)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_workshop_payments_status
            ON workshop_payments (telegram_id, status)
            WHERE status = 'success'
        ''')
        await conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workshop_payments_payment_id
            ON workshop_payments (payment_id)
            WHERE payment_id IS NOT NULL
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS community_members (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                username TEXT,
                first_name TEXT,
                source TEXT NOT NULL DEFAULT 'unknown',
                joined_at TIMESTAMP DEFAULT NOW(),
                left_at TIMESTAMP
            )
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_community_members_chat
            ON community_members (chat_id)
            WHERE left_at IS NULL
        ''')
        await conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_community_members_unique
            ON community_members (telegram_id, chat_id)
            WHERE left_at IS NULL
        ''')

        # ═══════════════════════════════════════════════════════════
        # ВНЕШНИЕ КЛИЕНТЫ (WP-411 Ф2): одноразовые коды + токены
        # ═══════════════════════════════════════════════════════════
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS external_auth_codes (
                code TEXT PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                account_id UUID NOT NULL,
                scope TEXT NOT NULL DEFAULT 'full',
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                expires_at TIMESTAMP NOT NULL
            )
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_eac_expires
            ON external_auth_codes (expires_at)
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS ory_client_tokens (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                account_id UUID NOT NULL,
                access_token_hash TEXT NOT NULL UNIQUE,
                refresh_token_hash TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL DEFAULT 'full',
                client_label TEXT DEFAULT 'Claude Code',
                last_used TIMESTAMP,
                revoked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
            )
        ''')
        await conn.execute('''
            ALTER TABLE ory_client_tokens
            DROP COLUMN IF EXISTS access_token_enc,
            DROP COLUMN IF EXISTS refresh_token_enc
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_oct_account
            ON ory_client_tokens (account_id)
            WHERE revoked_at IS NULL
        ''')

    logger.info("All tables created/updated")


async def create_tables_health(pool: asyncpg.Pool):
    """Создать таблицы в health БД (WP-268 Phase 5 G5 Tier2).

    Вызывается при первом подключении к health BD (Neon health).
    Идемпотентно: CREATE TABLE IF NOT EXISTS + ALTER ADD COLUMN IF NOT EXISTS.
    """
    async with pool.acquire() as conn:
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
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_error_logs_escalated
            ON error_logs (escalated, last_seen_at DESC)
        ''')

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
        # ТРЕЙСИНГ ЗАПРОСОВ (для Grafana, WP-7 Ф2)
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

    logger.info("Health BD tables created/updated")
