#!/usr/bin/env bash
# WP-418 Ф5 / Ф5.1: delivery-layer canary — queries notification_queue via railway ssh.
# railway run does NOT work from a local machine: it injects DATABASE_URL pointing at
# postgres.railway.internal, which resolves only inside the Railway network (socket.gaierror).
# railway ssh executes python3 INSIDE the service container, where the internal host resolves.
# Security Gate B7.7c: no direct secret access — DATABASE_URL never leaves the container.
#
# Usage:
#   bash scripts/check-delivery-canary.sh [--days N] [--service NAME] [--schema SCHEMA]
#
# Defaults (me_bot canary, Ф5):
#   --service aist_me_bot --schema development
#
# Example for pilot cohort (Ф5.1):
#   bash scripts/check-delivery-canary.sh --service aist_pilot_bot --schema development
set -euo pipefail

DAYS=7
SERVICE="aist_me_bot"
SCHEMA="development"

usage() {
    echo "Usage: $0 [--days N] [--service NAME] [--schema SCHEMA]" >&2
    echo "Defaults: --days 7 --service aist_me_bot --schema development" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --days)
            shift
            [[ $# -gt 0 ]] || usage
            DAYS="$1"
            shift
            ;;
        --service)
            shift
            [[ $# -gt 0 ]] || usage
            SERVICE="$1"
            shift
            ;;
        --schema)
            shift
            [[ $# -gt 0 ]] || usage
            SCHEMA="$1"
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

if ! [[ "$DAYS" =~ ^[0-9]+$ ]]; then
    echo "Error: --days must be a positive integer, got: $DAYS" >&2
    exit 1
fi
if [[ -z "$SERVICE" ]]; then
    echo "Error: --service must not be empty" >&2
    exit 1
fi
if ! [[ "$SCHEMA" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
    echo "Error: --schema must be a valid identifier (letters/digits/underscore), got: $SCHEMA" >&2
    exit 1
fi

echo "=== Delivery Canary (service=${SERVICE}, schema=${SCHEMA}, last ${DAYS} days) ==="

echo ""
echo "--- Sent by notification_class ---"
railway ssh --service "${SERVICE}" python3 - "$DAYS" "$SCHEMA" <<'PYEOF'
import asyncio, asyncpg, os, sys

days = int(sys.argv[1])
schema = sys.argv[2]

async def run():
    dsn = os.environ['DATABASE_URL']
    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch(f'''
        SELECT notification_class, count(*) AS sent
        FROM {schema}.notification_queue
        WHERE status = 'sent'
          AND created_at >= NOW() - INTERVAL '{days} days'
        GROUP BY notification_class
        ORDER BY sent DESC
    ''')
    for r in rows:
        print(f"  {r['notification_class']}: {r['sent']}")
    await conn.close()

asyncio.run(run())
PYEOF

echo ""
echo "--- Cap-exceeded suppressions by notification_class ---"
railway ssh --service "${SERVICE}" python3 - "$DAYS" "$SCHEMA" <<'PYEOF'
import asyncio, asyncpg, os, sys

days = int(sys.argv[1])
schema = sys.argv[2]

async def run():
    dsn = os.environ['DATABASE_URL']
    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch(f'''
        SELECT notification_class, count(*) AS cap_blocked
        FROM {schema}.notification_queue
        WHERE status = 'suppressed'
          AND reason = 'cap-exceeded'
          AND created_at >= NOW() - INTERVAL '{days} days'
        GROUP BY notification_class
        ORDER BY cap_blocked DESC
    ''')
    if rows:
        for r in rows:
            print(f"  {r['notification_class']}: {r['cap_blocked']}")
    else:
        print('  (none)')
    await conn.close()

asyncio.run(run())
PYEOF

echo ""
echo "=== Done ==="
