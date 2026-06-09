"""
Синхронизация marathon_status (Railway ↔ Neon) — WP-330 followup.

Neon learning.marathon_progress.status — источник истины для нового движка.
Railway development.user_state.marathon_status — legacy-поле, должно отражать Neon.

Маппинг Neon → Railway:
  registered → not_started  (не начал ещё)
  active     → active
  paused     → paused
  completed  → completed
  dropped    → not_started  (в Railway нет состояния dropped)

2026-06-09: Кими нашёл 27 расхождений и исправил вручную.
Этот скрипт делает то же самое воспроизводимо, с dry-run и аудитом.

Запуск:
    python -m scripts.wp330_sync_marathon_status           # dry-run (показать)
    python -m scripts.wp330_sync_marathon_status --apply   # применить
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATABASE_URL, LEARNING_URL

# Маппинг Neon status → Railway marathon_status.
NEON_TO_RAILWAY = {
    "registered": "not_started",
    "active":     "active",
    "paused":     "paused",
    "completed":  "completed",
    "dropped":    "not_started",
}

# Получаем все расхождения: пользователи есть в обеих БД, но статусы не совпадают.
DIFF_SQL = """
SELECT rw.chat_id,
       rw.marathon_status  AS railway_status,
       neon.status         AS neon_status
FROM   development.user_state rw
JOIN   public.users u ON u.telegram_id = rw.chat_id
-- JOIN через telegram_id в public.users → user_id в learning
JOIN   (
    SELECT mp.user_id, mp.status
    FROM   learning.marathon_progress mp
) neon ON neon.user_id = u.telegram_id
WHERE  rw.marathon_status IS DISTINCT FROM (
    CASE neon.status
        WHEN 'registered' THEN 'not_started'
        WHEN 'active'     THEN 'active'
        WHEN 'paused'     THEN 'paused'
        WHEN 'completed'  THEN 'completed'
        WHEN 'dropped'    THEN 'not_started'
        ELSE rw.marathon_status  -- неизвестный статус — не трогать
    END
)
ORDER BY rw.chat_id
"""


async def run(apply: bool = False) -> int:
    # Railway и Neon — две разные БД, нужны два соединения.
    rw_conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
    neon_conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)
    try:
        # Читаем расхождения из Railway (там есть оба поля через JOIN с Neon).
        # Но Neon и Railway — разные хосты, cross-DB JOIN невозможен через SQL.
        # Читаем каждую БД отдельно и сравниваем в Python.

        rw_rows = await rw_conn.fetch(
            """SELECT u.telegram_id AS user_id, s.marathon_status AS railway_status
               FROM development.user_state s
               JOIN public.users u ON u.telegram_id = s.chat_id
               WHERE s.marathon_status IS NOT NULL"""
        )
        neon_rows = await neon_conn.fetch(
            "SELECT user_id, status AS neon_status FROM learning.marathon_progress"
        )

        rw_map = {r["user_id"]: r["railway_status"] for r in rw_rows}
        neon_map = {r["user_id"]: r["neon_status"] for r in neon_rows}

        mismatches = []
        for user_id, neon_status in neon_map.items():
            if user_id not in rw_map:
                continue
            target = NEON_TO_RAILWAY.get(neon_status)
            if target is None:
                continue  # неизвестный Neon-статус — пропускаем
            current = rw_map[user_id]
            if current != target:
                mismatches.append({
                    "user_id":        user_id,
                    "railway_status": current,
                    "neon_status":    neon_status,
                    "target":         target,
                })

        if not mismatches:
            print("✅ Расхождений нет. Railway marathon_status совпадает с Neon у всех пользователей.")
            return 0

        print(f"⚠️  Найдено {len(mismatches)} расхождений:\n")
        print(f"{'user_id':>12} | {'Railway':>12} | {'Neon':>12} | {'→ Target':>12}")
        print("-" * 56)
        for m in mismatches:
            print(f"{m['user_id']:>12} | {m['railway_status']:>12} | {m['neon_status']:>12} | {m['target']:>12}")

        by_pair: dict[tuple, list] = {}
        for m in mismatches:
            key = (m["railway_status"], m["neon_status"])
            by_pair.setdefault(key, []).append(m["user_id"])

        print(f"\n📊 По типу расхождения:")
        for (rw, neon), ids in sorted(by_pair.items(), key=lambda x: -len(x[1])):
            target = NEON_TO_RAILWAY.get(neon, "?")
            print(f"  Railway={rw!r}, Neon={neon!r} → {target!r}: {len(ids)} чел.")

        if apply:
            for (_, neon_status), ids in by_pair.items():
                target = NEON_TO_RAILWAY.get(neon_status)
                if target is None:
                    continue
                result = await rw_conn.execute(
                    "UPDATE development.user_state SET marathon_status = $1 "
                    "WHERE chat_id = ANY($2::bigint[])",
                    target, ids,
                )
                updated = int(result.split()[-1])
                print(f"  Обновлено {updated} строк: Neon={neon_status!r} → Railway={target!r}")
            print(f"\n✅ Применено. Всего исправлено: {len(mismatches)} пользователей.")
        else:
            print(f"\nДля применения: python -m scripts.wp330_sync_marathon_status --apply")

        return 1
    finally:
        await rw_conn.close()
        await neon_conn.close()


if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    code = asyncio.run(run(apply=apply_mode))
    sys.exit(code)
