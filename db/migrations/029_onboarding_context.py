"""
Миграция 029: добавляет onboarding_context JSONB и onboarding_spot_check_due TIMESTAMPTZ
в таблицу development.user_state.

# see DP.SC.170, DP.SC.173, DP.ROLE.067, DP.ROLE.073

Источник: WP-406 Ф5, peer-session 2026-06-10-16-wp406-f5-f6-onboarder.

Решение о co-location (не отдельная таблица):
  R46 читает onboarding_spot_check_due в одном SELECT без JOIN.
  Тот же паттерн что marathon_status, feed_schedule_time.

Схема onboarding_context:
{
  "x2_status": "open|in_progress|closed",
  "x2_checklist": {"q1": false, "q2": false, "q3": false, "q4": false},
  "x3_status": "open|in_progress|closed",
  "x3_stage": null,
  "x3_bottleneck": null,
  "x3_course": null,
  "onboarding_complete": false,
  "t0_phase_done": false,
  "last_gap_check": null
}

onboarding_spot_check_due — вынесена из JSONB для индексируемого range-запроса:
  R46: WHERE onboarding_spot_check_due < NOW()

Запуск:
    python -m db.migrations.029_onboarding_context
"""

import asyncio
import asyncpg
import os


UP = """
ALTER TABLE development.user_state
    ADD COLUMN IF NOT EXISTS onboarding_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS onboarding_spot_check_due TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_user_state_spot_check_due
    ON development.user_state (onboarding_spot_check_due)
    WHERE onboarding_spot_check_due IS NOT NULL;

COMMENT ON COLUMN development.user_state.onboarding_context IS
    'Состояние онбординга Онбордера (DP.SC.170/DP.ROLE.067). '
    'Ключи: x2_status, x2_checklist, x3_status, x3_stage, x3_bottleneck, x3_course, '
    'onboarding_complete, t0_phase_done, last_gap_check. '
    'Source: WP-406 Ф5.';

COMMENT ON COLUMN development.user_state.onboarding_spot_check_due IS
    'Дата следующей X2 spot-check проверки (R46). '
    'Вынесена из JSONB для индексируемого range-запроса: WHERE onboarding_spot_check_due < NOW(). '
    'Source: WP-406 Ф5.';
"""

DOWN = """
DROP INDEX IF EXISTS idx_user_state_spot_check_due;
ALTER TABLE development.user_state
    DROP COLUMN IF EXISTS onboarding_spot_check_due,
    DROP COLUMN IF EXISTS onboarding_context;
"""


async def run(dsn: str | None = None) -> None:
    dsn = dsn or os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(UP)
        print("029_onboarding_context: OK")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
