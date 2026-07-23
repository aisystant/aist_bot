"""
Migration 039: IDCOL1 Ф2 (WP-7) — drop public.users.dt_user_id.

Consolidates identity to a single canonical column (ory_id). Stage 1
(2026-07-06/09) switched every read-path to `u.ory_id::text AS dt_user_id`
and promoted to prod 09.07 — 14 days stable, 0 identity regressions as of
2026-07-23. This migration is Stage 2: the physical column, its unique
constraint (users_dt_user_id_key) and the dependent user_knowledge_profile
VIEW are removed.

MUST run AFTER the code deploy of this same commit has completed at least
one full bot restart (db/models.py's create_tables() drops the VIEW on
boot — running this migration before that restart hits a dependency error;
running it after an OLD instance is still alive risks a crash-loop, see
db/models.py comment). Practically: deploy → confirm clean restart in
Railway logs → then run this script.

Run (against pilot first, then prod — same script, different DATABASE_URL):
    python -m db.migrations.039_idcol1_drop_dt_user_id
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL
import asyncpg


async def migrate():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")

    print(f"Подключение к bot DB...")
    conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)

    try:
        # Pre-flight: confirm Stage 1 held — 0 rows where ory_id/dt_user_id disagree.
        divergent = await conn.fetchval("""
            SELECT count(*) FROM public.users
            WHERE (ory_id IS NOT NULL OR dt_user_id IS NOT NULL)
              AND ory_id::text IS DISTINCT FROM dt_user_id
        """)
        if divergent:
            raise RuntimeError(
                f"{divergent} divergent rows between ory_id and dt_user_id — "
                "refusing to drop the column. Investigate before re-running."
            )
        print(f"  Pre-flight: 0 divergent rows — OK")

        view_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.views "
            "WHERE table_schema = 'public' AND table_name = 'user_knowledge_profile')"
        )
        if view_exists:
            raise RuntimeError(
                "user_knowledge_profile VIEW still exists — the code deploy that drops it "
                "(db/models.py create_tables()) has not run yet on this DB. Deploy first, "
                "confirm a clean bot restart in Railway logs, then re-run this migration."
            )
        print(f"  Pre-flight: user_knowledge_profile VIEW already gone — OK")

        async with conn.transaction():
            await conn.execute("ALTER TABLE public.users DROP COLUMN dt_user_id")
            print("  ALTER TABLE public.users DROP COLUMN dt_user_id — OK "
                  "(implicitly drops users_dt_user_id_key UNIQUE constraint)")

        print("\n✅ Migration 039 done — dt_user_id removed, ory_id is the sole identity column")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
