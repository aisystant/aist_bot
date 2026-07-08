"""
Migration 038: align learning.onboarding_state + learning.marathon_queue on
Railway pilot-bot Postgres with their canonical Neon migrations (WP-117 Ф-onboarding-gap).

Root cause (peer-session 2026-07-08-01-wp117-railway-column-drift): migration 025
(learning_schema_railway.py) bootstrapped these tables from its own inline DDL, which
never tracked later canonical migrations:
  - onboarding_state: 025 shipped 16 columns; canonical 233-wp346-onboarding-state.sql
    (neon-migrations repo) has 29. 14 columns missing on Railway, incl. last_nudge_at
    (blocked PR #291 cooldown), has_subscription, all first_use_*, slot_count,
    activity_days_count, consecutive_silence_days, last_slot_at, consent_at.
  - marathon_queue: 025 never got bot_id (added later by canonical
    240-wp7-mar5-marathon-queue-bot-id.sql).

This migration fixes the LIVE Railway table (one-time, run once). 025 itself is fixed
separately so future from-scratch bootstraps do not reintroduce the same drift.

Pre-flight: verified 2026-07-08 via direct SELECT against Railway — onboarding_state
has 0 rows, 0 FK (either direction), 0 triggers, 0 views, 0 RLS policies. DROP has
nothing to break. Schema-only pg_dump snapshot taken before running this migration:
DS-my-strategy/sessions/2026-07/2026-07-08-01-wp117-railway-column-drift/pre-flight-snapshot/.

RLS: onboarding_state gets ENABLE ROW LEVEL SECURITY (matches 233) with no permissive
policy (default-deny, matches 233's intent). Verified harmless: the only role that reads
this table on Railway is `postgres` (superuser + BYPASSRLS) — RLS never applies to it.
The other 3 Railway roles (bot_admin, directus_admin, fsm_admin) have zero grants on the
learning schema today. If a read-only role is ever granted SELECT here, it will see 0
rows until a policy is added — that's an intentional feature of 233's design, not a bug.

Run:
    python -m db.migrations.038_wp117_onboarding_state_align
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import LEARNING_URL


async def migrate():
    if "rlwy.net" not in LEARNING_URL:
        raise RuntimeError(
            "LEARNING_URL does not look like a Railway host — refusing to run. "
            "This migration targets the pilot-bot Railway Postgres only, never Neon."
        )

    print("Подключение к learning-БД (LEARNING_URL)...")
    conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)

    try:
        async with conn.transaction():
            # ═══════════════════════════════════════════════════════════
            # 1. onboarding_state — DROP + recreate per canonical 233 (29 cols)
            # ═══════════════════════════════════════════════════════════
            row_count = await conn.fetchval("SELECT count(*) FROM learning.onboarding_state")
            if row_count:
                raise RuntimeError(
                    f"learning.onboarding_state has {row_count} rows — expected 0. "
                    "Refusing to DROP a non-empty table. Re-check before proceeding."
                )

            await conn.execute("DROP TABLE learning.onboarding_state")
            print("  DROP learning.onboarding_state — OK (was empty)")

            await conn.execute("""
                CREATE TABLE learning.onboarding_state (
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
            print("  CREATE learning.onboarding_state (29 cols, canonical 233) — OK")

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
            print("  Indexes (4) — OK")

            await conn.execute("ALTER TABLE learning.onboarding_state ENABLE ROW LEVEL SECURITY")
            print("  ENABLE ROW LEVEL SECURITY (default-deny, no policy — see module docstring) — OK")

            await conn.execute("""
                CREATE OR REPLACE FUNCTION learning.onboarding_state_update_ts()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = NOW();
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
            """)
            await conn.execute(
                "DROP TRIGGER IF EXISTS trg_onboarding_state_updated_at ON learning.onboarding_state"
            )
            await conn.execute("""
                CREATE TRIGGER trg_onboarding_state_updated_at
                    BEFORE UPDATE ON learning.onboarding_state
                    FOR EACH ROW EXECUTE FUNCTION learning.onboarding_state_update_ts()
            """)
            print("  updated_at trigger — OK")

            await conn.execute("""
                COMMENT ON TABLE learning.onboarding_state IS
                'WP-346 Ф1 + WP-117 Ф-onboarding-gap: состояние пилота в онбординговом пути. '
                'Writer = onboarding-controller.py. account_id = косвенно PII -> RLS (default-deny, '
                'no permissive policy — only postgres/BYPASSRLS roles read this table today).'
            """)

            role_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onboarding_controller')"
            )
            if role_exists:
                await conn.execute("GRANT USAGE ON SCHEMA learning TO onboarding_controller")
                await conn.execute(
                    "GRANT SELECT, INSERT, UPDATE ON learning.onboarding_state TO onboarding_controller"
                )
                print("  GRANT to onboarding_controller — OK")
            else:
                print("  onboarding_controller role not present on Railway — GRANT skipped (expected)")

            # ═══════════════════════════════════════════════════════════
            # 2. marathon_queue — add bot_id (canonical 240-wp7-mar5)
            # ═══════════════════════════════════════════════════════════
            await conn.execute(
                "ALTER TABLE learning.marathon_queue ADD COLUMN IF NOT EXISTS bot_id TEXT DEFAULT NULL"
            )
            await conn.execute("""
                COMMENT ON COLUMN learning.marathon_queue.bot_id IS
                'Bot identifier for isolation (pilot vs prod). NULL = legacy rows, visible to all bots. WP-7 MAR5.'
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_marathon_queue_pending_bot "
                "ON learning.marathon_queue (bot_id, scheduled_at) WHERE status = 'pending'"
            )
            print("  learning.marathon_queue.bot_id + index — OK")

            print("\n✅ Migration 038 done — onboarding_state aligned to 233, marathon_queue.bot_id added")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
