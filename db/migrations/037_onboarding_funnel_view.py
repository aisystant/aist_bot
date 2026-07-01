"""
Migration 037: standing onboarding funnel view (WP-406 Ф17 SQL-C1).

Creates development.onboarding_funnel — a daily funnel slice over the analytics
events shipped in Ф17 PR-2 (onboarding_started -> x2_completed -> x3_completed ->
onboarding_completed). Removes the "no ready slice -> manual ad-hoc SQL -> wrong
count" error class that produced the 23 Jun "0 participants" misread.

Grouping is by day + entry_type + lang: these three keys are present on the
started/x2/x3 payloads. cohort_id lives on started/completed only (x2/x3 do not
carry it yet), so it is intentionally left out of the grouping to avoid split
rows; add it once x2/x3 payloads include cohort_id too.

Period aggregation = sum across the day rows in the wanted range, e.g.:
    SELECT entry_type,
           SUM(started), SUM(x2_completed), SUM(x3_completed), SUM(completed)
    FROM development.onboarding_funnel
    WHERE day BETWEEN '2026-07-01' AND '2026-07-08'
    GROUP BY entry_type;

CLAUDE.md §10.22: CREATE OR REPLACE VIEW is forbidden -> DROP + CREATE.

Run:
    python -m db.migrations.037_onboarding_funnel_view
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


VIEW_SQL = """
CREATE VIEW development.onboarding_funnel AS
WITH ev AS (
    SELECT
        created_at::date          AS day,
        payload->>'entry_type'    AS entry_type,
        payload->>'lang'          AS lang,
        payload->>'cohort_id'     AS cohort_id,
        event_type
    FROM development.user_events
    WHERE event_type IN (
        'onboarding_started', 'x2_completed', 'x3_completed', 'onboarding_completed'
    )
)
SELECT
    day,
    COALESCE(entry_type, 'unknown')                             AS entry_type,
    COALESCE(lang, 'ru')                                        AS lang,
    COUNT(*) FILTER (WHERE event_type = 'onboarding_started')   AS started,
    COUNT(*) FILTER (WHERE event_type = 'x2_completed')         AS x2_completed,
    COUNT(*) FILTER (WHERE event_type = 'x3_completed')         AS x3_completed,
    COUNT(*) FILTER (WHERE event_type = 'onboarding_completed') AS completed
FROM ev
GROUP BY day, COALESCE(entry_type, 'unknown'), COALESCE(lang, 'ru')
ORDER BY day DESC, entry_type, lang
"""


async def migrate():
    conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
    try:
        async with conn.transaction():
            await conn.execute("DROP VIEW IF EXISTS development.onboarding_funnel")
            print("  DROP VIEW development.onboarding_funnel — OK")

            await conn.execute(VIEW_SQL)
            print("  CREATE VIEW development.onboarding_funnel — OK")

        print("Migration 037 done")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
