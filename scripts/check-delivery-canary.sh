#!/usr/bin/env bash
# WP-418 Ф5: delivery-layer canary — queries notification_queue via railway run.
# Security Gate B7.7c: uses railway run (no direct secret access).
# Usage: bash scripts/check-delivery-canary.sh [--days N]
set -euo pipefail

DAYS=7
if [[ "${1:-}" == "--days" && -n "${2:-}" ]]; then
    DAYS="$2"
fi
if ! [[ "$DAYS" =~ ^[0-9]+$ ]]; then
    echo "Error: --days must be a positive integer, got: $DAYS" >&2
    exit 1
fi

echo "=== Delivery Canary (last ${DAYS} days) ==="

echo ""
echo "--- Sent by notification_class ---"
railway run --service aist_me_bot python3 -c "
import asyncio, asyncpg, os

async def run():
    dsn = os.environ['DATABASE_URL']
    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch('''
        SELECT notification_class, count(*) AS sent
        FROM development.notification_queue
        WHERE status = \\'sent\\'
          AND created_at >= NOW() - INTERVAL \\'${DAYS} days\\'
        GROUP BY notification_class
        ORDER BY sent DESC
    ''')
    for r in rows:
        print(f\"  {r['notification_class']}: {r['sent']}\")
    await conn.close()

asyncio.run(run())
"

echo ""
echo "--- Cap-exceeded suppressions by notification_class ---"
railway run --service aist_me_bot python3 -c "
import asyncio, asyncpg, os

async def run():
    dsn = os.environ['DATABASE_URL']
    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch('''
        SELECT notification_class, count(*) AS cap_blocked
        FROM development.notification_queue
        WHERE status = \\'suppressed\\'
          AND reason = \\'cap-exceeded\\'
          AND created_at >= NOW() - INTERVAL \\'${DAYS} days\\'
        GROUP BY notification_class
        ORDER BY cap_blocked DESC
    ''')
    if rows:
        for r in rows:
            print(f\"  {r['notification_class']}: {r['cap_blocked']}\")
    else:
        print('  (none)')
    await conn.close()

asyncio.run(run())
"

echo ""
echo "=== Done ==="
