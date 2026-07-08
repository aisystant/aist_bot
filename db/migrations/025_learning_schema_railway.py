"""
Миграция 025: learning-схема для Railway Postgres (Ф-Pilot-LearningDB-Isolation, WP-7).

Цель: создать все learning-таблицы на Railway Postgres пилота, чтобы
  LEARNING_URL можно было переключить с Neon на Railway Postgres (DATABASE_URL).
  После переключения pilot-бот пишет marathon_queue/progress/state в Railway,
  а не в общий Neon — исчезает cross-contamination с prod.

Идемпотентность: все CREATE используют IF NOT EXISTS; VIEW — DROP + CREATE.
Порядок: сначала schema + public-таблицы, потом learning.* (т.к. 023/024
  тоже работают с learning-pool и запускаются после).

Таблицы (в LEARNING pool, public schema или learning schema):
  public:   domain_event, security_reject_log, bridge_2_cursors,
            content_cache, reminder, feed_sessions (verify-check)
  learning: marathon_queue, marathon_progress, marathon_state,
            marathon_activity, tracking_consent, stage_transitions,
            cp_assessments, onboarding_state
  views:    learning.marathon_stats, learning.cp_invalidation_signals

Запуск вручную:
    python -m db.migrations.025_learning_schema_railway
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import LEARNING_URL


async def migrate():
    print("Подключение к learning-БД...")
    conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)

    try:
        # ═══════════════════════════════════════════════════════════════════
        # 0. Schema
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("CREATE SCHEMA IF NOT EXISTS learning")
        print("  CREATE SCHEMA IF NOT EXISTS learning — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 1. public: domain_event (единый канал событий — DP.SC.020)
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS domain_event (
                id              BIGSERIAL PRIMARY KEY,
                source          TEXT NOT NULL,
                external_id     TEXT NOT NULL,
                event_type      TEXT NOT NULL,
                schema_version  TEXT NOT NULL DEFAULT 'v1',
                payload         JSONB NOT NULL,
                account_id      UUID,
                occurred_at     TIMESTAMPTZ NOT NULL,
                ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (source, external_id)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_domain_event_type ON domain_event (event_type)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_domain_event_account "
            "ON domain_event (account_id) WHERE account_id IS NOT NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_domain_event_occurred "
            "ON domain_event (occurred_at DESC)"
        )
        # NOTIFY trigger — создаём если функция ещё не существует
        await conn.execute("""
            CREATE OR REPLACE FUNCTION notify_domain_event_added()
            RETURNS TRIGGER AS $$
            BEGIN
                PERFORM pg_notify('domain_event_added', NEW.id::text);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        trigger_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_domain_event_notify'
            )
        """)
        if not trigger_exists:
            await conn.execute("""
                CREATE TRIGGER trg_domain_event_notify
                    AFTER INSERT ON domain_event
                    FOR EACH ROW
                    EXECUTE FUNCTION notify_domain_event_added()
            """)
        print("  domain_event — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 2. public: security_reject_log
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS security_reject_log (
                id          BIGSERIAL PRIMARY KEY,
                source      TEXT NOT NULL,
                external_id TEXT,
                reason      TEXT NOT NULL,
                details     JSONB,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_security_reject_reason "
            "ON security_reject_log (reason)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_security_reject_received "
            "ON security_reject_log (received_at DESC)"
        )
        print("  security_reject_log — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 3. public: bridge_2_cursors
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bridge_2_cursors (
                poller_name     TEXT PRIMARY KEY,
                last_seen_id    BIGINT NOT NULL DEFAULT 0,
                last_polled_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        print("  bridge_2_cursors — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 4. public: content_cache
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS content_cache (
                cache_key    TEXT PRIMARY KEY,
                content_type TEXT NOT NULL,
                content      TEXT NOT NULL,
                expires_at   TIMESTAMPTZ NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_cache_expires "
            "ON content_cache (expires_at)"
        )
        print("  content_cache — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 5. public: reminder (WP-212 bot_id migration target)
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminder (
                id            SERIAL PRIMARY KEY,
                chat_id       BIGINT,
                reminder_type TEXT,
                scheduled_for TIMESTAMP,
                sent          BOOLEAN DEFAULT FALSE,
                created_at    TIMESTAMP DEFAULT NOW(),
                fail_count    INTEGER DEFAULT 0,
                bot_id        BIGINT
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminder_pending "
            "ON reminder (scheduled_for) WHERE sent = FALSE"
        )
        print("  reminder — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 6. public: feed_sessions (schema verify check — non-fatal if missing)
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_sessions (
                id          SERIAL PRIMARY KEY,
                chat_id     BIGINT NOT NULL,
                started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ended_at    TIMESTAMPTZ,
                topic_id    TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        print("  feed_sessions — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 7. learning.marathon_queue
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS learning.marathon_queue (
                id           BIGSERIAL  PRIMARY KEY,
                user_id      BIGINT     NOT NULL,
                day_number   INTEGER    NOT NULL CHECK (day_number BETWEEN 1 AND 14),
                content_type TEXT       NOT NULL DEFAULT 'lesson'
                             CHECK (content_type IN ('lesson', 'practice', 'checkin')),
                content_ref  TEXT,
                content_text TEXT,
                status       TEXT       NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'sent', 'failed')),
                scheduled_at TIMESTAMPTZ NOT NULL,
                sent_at      TIMESTAMPTZ,
                attempts     INTEGER    NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                error        TEXT,
                bot_id       TEXT       DEFAULT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, day_number, content_type)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_marathon_queue_pending "
            "ON learning.marathon_queue (scheduled_at) WHERE status = 'pending'"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_marathon_queue_user "
            "ON learning.marathon_queue (user_id, status)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_marathon_queue_pending_bot "
            "ON learning.marathon_queue (bot_id, scheduled_at) WHERE status = 'pending'"
        )
        await conn.execute("""
            COMMENT ON COLUMN learning.marathon_queue.bot_id IS
            'Bot identifier for isolation (pilot vs prod). NULL = legacy rows, visible to all bots. WP-7 MAR5.'
        """)
        print("  learning.marathon_queue — OK (incl. bot_id, WP-7 MAR5)")

        # ═══════════════════════════════════════════════════════════════════
        # 8. learning.marathon_progress
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS learning.marathon_progress (
                user_id       BIGINT    PRIMARY KEY,
                current_day   INTEGER   NOT NULL DEFAULT 0
                              CHECK (current_day BETWEEN 0 AND 14),
                status        TEXT      NOT NULL DEFAULT 'registered'
                              CHECK (status IN ('registered', 'active', 'paused', 'completed', 'dropped')),
                started_at    TIMESTAMPTZ,
                completed_at  TIMESTAMPTZ,
                total_checkins INTEGER  NOT NULL DEFAULT 0,
                missed_days   INTEGER   NOT NULL DEFAULT 0,
                badge_list    TEXT[]    NOT NULL DEFAULT '{}',
                nudge_variant TEXT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_marathon_progress_status "
            "ON learning.marathon_progress (status) "
            "WHERE status IN ('active', 'paused')"
        )
        print("  learning.marathon_progress — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 9. learning.marathon_state
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS learning.marathon_state (
                id         BIGSERIAL  PRIMARY KEY,
                user_id    BIGINT     NOT NULL,
                day        INTEGER    NOT NULL CHECK (day BETWEEN 1 AND 14),
                state      TEXT       NOT NULL DEFAULT 'unknown'
                           CHECK (state IN ('chaos', 'stuck', 'turn', 'unknown')),
                check_in_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                notes      TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, day)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_marathon_state_user "
            "ON learning.marathon_state (user_id, day)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_marathon_state_day "
            "ON learning.marathon_state (day, state)"
        )
        print("  learning.marathon_state — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 10. learning.marathon_activity
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS learning.marathon_activity (
                user_id       BIGINT     NOT NULL,
                activity_date DATE       NOT NULL,
                action_type   TEXT       NOT NULL DEFAULT 'checkin',
                raw_count     INTEGER    NOT NULL DEFAULT 1,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, activity_date, action_type)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_marathon_activity_user_date "
            "ON learning.marathon_activity (user_id, activity_date)"
        )
        print("  learning.marathon_activity — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 11. learning.marathon_stats (view — DROP + CREATE для идемпотентности)
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("DROP VIEW IF EXISTS learning.marathon_stats")
        await conn.execute("""
            CREATE VIEW learning.marathon_stats AS
            SELECT
                mp.user_id,
                mp.current_day,
                mp.status,
                mp.started_at,
                mp.completed_at,
                COUNT(DISTINCT ms.day) AS total_checkins,
                GREATEST(mp.current_day - COUNT(DISTINCT ms.day), 0) AS missed_days,
                mp.badge_list,
                COUNT(ms.day) FILTER (WHERE ms.state = 'chaos') AS chaos_days,
                COUNT(ms.day) FILTER (WHERE ms.state = 'stuck') AS stuck_days,
                COUNT(ms.day) FILTER (WHERE ms.state = 'turn')  AS turn_days,
                MAX(ms.check_in_at) AS last_check_in_at,
                mp.updated_at
            FROM learning.marathon_progress mp
            LEFT JOIN learning.marathon_state ms ON ms.user_id = mp.user_id
            GROUP BY mp.user_id
        """)
        print("  learning.marathon_stats (view) — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 12. learning.tracking_consent
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS learning.tracking_consent (
                account_id UUID     PRIMARY KEY,
                opt_in     BOOLEAN  NOT NULL DEFAULT TRUE,
                opted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                scope      TEXT[]   DEFAULT ARRAY['stage_evaluation', 'club_activity']
            )
        """)
        print("  learning.tracking_consent — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 13. learning.stage_transitions (нужна до cp_assessments)
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS learning.stage_transitions (
                id           UUID       PRIMARY KEY DEFAULT gen_random_uuid(),
                account_id   UUID       NOT NULL,
                from_stage   SMALLINT   NOT NULL CHECK (from_stage BETWEEN 0 AND 5),
                to_stage     SMALLINT   NOT NULL CHECK (to_stage BETWEEN 1 AND 5),
                triggered_by TEXT       NOT NULL
                             CHECK (triggered_by IN (
                                 'SR.001', 'SR.002', 'SR.003', 'SR.004', 'manual_calibration'
                             )),
                occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                evidence     JSONB,
                CHECK (to_stage > from_stage)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage_trans_account "
            "ON learning.stage_transitions (account_id, occurred_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage_trans_occurred "
            "ON learning.stage_transitions (occurred_at DESC)"
        )
        # cp_assessment_id FK добавляется после создания cp_assessments
        print("  learning.stage_transitions — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 14. learning.cp_assessments
        #     recommended_stream без CHECK (бот пишет 'РР' для stage=5, WP-371)
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS learning.cp_assessments (
                id                  BIGSERIAL   PRIMARY KEY,
                account_id          UUID        NOT NULL,
                stage               SMALLINT    NOT NULL CHECK (stage BETWEEN 1 AND 5),
                bottleneck_slot     TEXT,
                recommended_stream  TEXT,
                skip_to_stage       SMALLINT    CHECK (skip_to_stage BETWEEN 1 AND 5),
                cp_scores           JSONB       NOT NULL DEFAULT '{}',
                source              TEXT        NOT NULL
                                    CHECK (source IN ('dialogue', 'bh_proxy', 'import')),
                interface           TEXT        NOT NULL
                                    CHECK (interface IN ('tg', 'web', 'vscode', 'background')),
                questions_count     SMALLINT,
                rcs_version         TEXT        DEFAULT 'v5.0',
                assessed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                valid_until         TIMESTAMPTZ,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cp_assessments_account_latest "
            "ON learning.cp_assessments (account_id, assessed_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cp_assessments_valid "
            "ON learning.cp_assessments (account_id, valid_until) "
            "WHERE valid_until IS NOT NULL"
        )
        # FK: stage_transitions.cp_assessment_id → cp_assessments.id
        await conn.execute("""
            ALTER TABLE learning.stage_transitions
            ADD COLUMN IF NOT EXISTS cp_assessment_id BIGINT
                REFERENCES learning.cp_assessments(id) ON DELETE SET NULL
        """)
        print("  learning.cp_assessments — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 15. learning.cp_invalidation_signals (view)
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("DROP VIEW IF EXISTS learning.cp_invalidation_signals")
        await conn.execute("""
            CREATE VIEW learning.cp_invalidation_signals AS
            SELECT
                a.account_id,
                a.id           AS assessment_id,
                a.stage,
                a.bottleneck_slot,
                a.assessed_at,
                a.valid_until,
                NOW() - a.assessed_at AS age,
                CASE
                    WHEN a.valid_until < NOW() THEN 'ttl_expired'
                    WHEN NOW() - a.assessed_at > INTERVAL '90 days' THEN 'age_90d'
                    ELSE NULL
                END AS invalidation_reason
            FROM learning.cp_assessments a
            WHERE a.id = (
                SELECT id FROM learning.cp_assessments
                WHERE account_id = a.account_id
                ORDER BY assessed_at DESC
                LIMIT 1
            )
            AND (
                a.valid_until < NOW()
                OR NOW() - a.assessed_at > INTERVAL '90 days'
            )
        """)
        print("  learning.cp_invalidation_signals (view) — OK")

        # ═══════════════════════════════════════════════════════════════════
        # 16. learning.onboarding_state
        #     WP-117 Ф-onboarding-gap (2026-07-08): kept 1:1 with canonical
        #     neon-migrations/mvp/233-wp346-onboarding-state.sql — this table
        #     drifted from it once already (16 cols vs 29), fixed live via
        #     migration 038. Keep both in sync when either changes.
        # ═══════════════════════════════════════════════════════════════════
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS learning.onboarding_state (
                account_id              UUID        PRIMARY KEY,
                cohort_id               TEXT        NOT NULL DEFAULT 'R1',

                level                   SMALLINT    NOT NULL DEFAULT 1
                                        CHECK (level BETWEEN 1 AND 4),

                msg_0_sent_at           TIMESTAMPTZ,
                msg_1_sent_at           TIMESTAMPTZ,
                msg_2_sent_at           TIMESTAMPTZ,
                msg_3_sent_at           TIMESTAMPTZ,
                msg_4_sent_at           TIMESTAMPTZ,
                msg_5_sent_at           TIMESTAMPTZ,
                msg_6_sent_at           TIMESTAMPTZ,
                msg_7_sent_at           TIMESTAMPTZ,
                msg_8_sent_at           TIMESTAMPTZ,
                msg_9_sent_at           TIMESTAMPTZ,
                msg_10_sent_at          TIMESTAMPTZ,

                slot_count              INT         NOT NULL DEFAULT 0,
                activity_days_count     INT         NOT NULL DEFAULT 0,
                last_slot_at            TIMESTAMPTZ,
                consecutive_silence_days INT        NOT NULL DEFAULT 0,

                first_use_consent       BOOLEAN     NOT NULL DEFAULT FALSE,
                first_use_slot          BOOLEAN     NOT NULL DEFAULT FALSE,
                first_use_points        BOOLEAN     NOT NULL DEFAULT FALSE,
                first_use_connect_browser BOOLEAN   NOT NULL DEFAULT FALSE,
                first_use_guide_render  BOOLEAN     NOT NULL DEFAULT FALSE,
                first_use_space_open    BOOLEAN     NOT NULL DEFAULT FALSE,
                first_use_connect_full  BOOLEAN     NOT NULL DEFAULT FALSE,

                has_subscription        BOOLEAN     NOT NULL DEFAULT FALSE,
                last_nudge_at           TIMESTAMPTZ,

                consent_at              TIMESTAMPTZ,
                updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_onboarding_state_cohort "
            "ON learning.onboarding_state (cohort_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_onboarding_state_level "
            "ON learning.onboarding_state (level)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_onboarding_state_slot_count "
            "ON learning.onboarding_state (slot_count) WHERE msg_5_sent_at IS NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_onboarding_state_updated "
            "ON learning.onboarding_state (updated_at DESC)"
        )
        await conn.execute("ALTER TABLE learning.onboarding_state ENABLE ROW LEVEL SECURITY")
        await conn.execute("""
            CREATE OR REPLACE FUNCTION learning.onboarding_state_update_ts()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        await conn.execute("DROP TRIGGER IF EXISTS trg_onboarding_state_updated_at ON learning.onboarding_state")
        await conn.execute("""
            CREATE TRIGGER trg_onboarding_state_updated_at
                BEFORE UPDATE ON learning.onboarding_state
                FOR EACH ROW EXECUTE FUNCTION learning.onboarding_state_update_ts()
        """)
        await conn.execute("""
            COMMENT ON TABLE learning.onboarding_state IS
            'WP-346 Ф1 + WP-117 Ф-onboarding-gap: состояние пилота в онбординговом пути. '
            'Writer = onboarding-controller.py. account_id = косвенно PII -> RLS (default-deny, '
            'no permissive policy — only postgres/BYPASSRLS roles read this table today).'
        """)
        print("  learning.onboarding_state — OK (29 cols, canonical 233, RLS + trigger)")

        print("\n✅ Миграция 025 завершена — learning-схема на Railway Postgres готова")

    finally:
        await conn.close()


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    """Идемпотентная проверка + создание learning-схемы.

    Вызывается из db/models.py при старте бота.
    Проверяет наличие ключевых таблиц; если они отсутствуют — запускает полную миграцию.
    Возвращает True если что-то было создано.
    """
    # Быстрая проверка: есть ли основные таблицы?
    async with pool.acquire() as conn:
        queue_exists = await conn.fetchval(
            "SELECT to_regclass('learning.marathon_queue')"
        )
        domain_exists = await conn.fetchval(
            "SELECT to_regclass('domain_event')"
        )

    if queue_exists and domain_exists:
        return False  # Уже есть — пропускаем

    await migrate()
    return True


if __name__ == "__main__":
    asyncio.run(migrate())
