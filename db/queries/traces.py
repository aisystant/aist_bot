"""
Запросы для таблицы request_traces (трейсинг запросов).

Пороговые значения (мс):
  Навигация (/mode, /help, /profile, /settings, /language, cb:*):
    green <1000, yellow <3000, red >=3000
  Тяжёлые (/feed, /learn, /test, /assessment):
    green <3000, yellow <8000, red >=8000
  Консультант (msg:?*):
    green <8000, yellow <20000, red >=20000
"""

from typing import List, Optional
from db.connection import acquire
from config import get_logger

logger = get_logger(__name__)

# --- Thresholds (ms) ---
# (green_max, yellow_max) — выше yellow_max = red
THRESHOLDS = {
    'nav':          (1000, 3000),    # /mode, /help, /profile, cb:*
    'heavy':        (3000, 8000),    # /feed, /learn, /test
    'consultation': (8000, 20000),   # msg:?*
}

_NAV_COMMANDS = {'/mode', '/help', '/profile', '/settings', '/language', '/mydata', '/feedback'}
_HEAVY_COMMANDS = {'/feed', '/learn', '/test', '/assessment'}


def classify_command(command: str) -> str:
    """Classify command into threshold category."""
    if not command:
        return 'nav'
    cmd = command.split()[0] if command else ''
    if cmd.startswith('cb:'):
        return 'nav'
    if cmd.startswith('msg:?') or cmd.startswith('msg:? '):
        return 'consultation'
    if cmd in _HEAVY_COMMANDS:
        return 'heavy'
    return 'nav'


def get_color(total_ms: float, category: str) -> str:
    """Return traffic light emoji for given latency."""
    green_max, yellow_max = THRESHOLDS.get(category, THRESHOLDS['nav'])
    if total_ms <= green_max:
        return '\U0001f7e2'  # green
    elif total_ms <= yellow_max:
        return '\U0001f7e1'  # yellow
    else:
        return '\U0001f534'  # red


async def cleanup_old_traces(days: int = 7) -> int:
    """Удалить traces старше N дней. Возвращает количество удалённых."""
    async with await acquire() as conn:
        result = await conn.execute(
            "DELETE FROM request_traces WHERE created_at < NOW() - INTERVAL '1 day' * $1",
            days,
        )
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"[Traces] Cleaned up {count} traces older than {days} days")
        return count


async def get_latency_report(hours: int = 24) -> dict:
    """Get latency report for the last N hours.

    Returns dict with:
      - summary: {total_requests, avg_ms, p95_ms, red_count}
      - by_command: [{command, avg_ms, p95_ms, max_ms, count, color}]
      - red_traces: [{command, total_ms, state, created_at}] (last 5 red)
      - slowest_spans: [{name, avg_ms, max_ms, count}]
    """
    async with await acquire() as conn:
        # Summary
        summary = await conn.fetchrow("""
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(total_ms)::int, 0) AS avg_ms,
                   COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms)::int, 0) AS p95_ms
            FROM request_traces
            WHERE created_at > NOW() - INTERVAL '1 hour' * $1
        """, hours)

        # By command
        by_command = await conn.fetch("""
            SELECT command,
                   AVG(total_ms)::int AS avg_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms)::int AS p95_ms,
                   MAX(total_ms)::int AS max_ms,
                   COUNT(*) AS count
            FROM request_traces
            WHERE created_at > NOW() - INTERVAL '1 hour' * $1
            GROUP BY command
            ORDER BY avg_ms DESC
        """, hours)

        # Slowest spans
        slowest_spans = await conn.fetch("""
            SELECT s->>'name' AS name,
                   AVG((s->>'duration_ms')::numeric)::int AS avg_ms,
                   MAX((s->>'duration_ms')::numeric)::int AS max_ms,
                   COUNT(*) AS count
            FROM request_traces, jsonb_array_elements(spans) AS s
            WHERE created_at > NOW() - INTERVAL '1 hour' * $1
            GROUP BY name
            ORDER BY avg_ms DESC
            LIMIT 10
        """, hours)

        # Count red traces
        all_traces = await conn.fetch("""
            SELECT command, total_ms, state, created_at
            FROM request_traces
            WHERE created_at > NOW() - INTERVAL '1 hour' * $1
            ORDER BY created_at DESC
        """, hours)

    # Classify and find red
    red_traces = []
    red_count = 0
    for t in all_traces:
        cat = classify_command(t['command'])
        _, yellow_max = THRESHOLDS.get(cat, THRESHOLDS['nav'])
        if t['total_ms'] > yellow_max:
            red_count += 1
            if len(red_traces) < 5:
                red_traces.append(dict(t))

    return {
        'summary': dict(summary) if summary else {'total': 0, 'avg_ms': 0, 'p95_ms': 0},
        'red_count': red_count,
        'by_command': [dict(r) for r in by_command],
        'red_traces': red_traces,
        'slowest_spans': [dict(r) for r in slowest_spans],
    }


async def check_latency_alerts(minutes: int = 15) -> Optional[str]:
    """Check recent traces for red-zone violations.

    Returns alert message (HTML) if there are red-zone requests, None otherwise.
    """
    async with await acquire() as conn:
        rows = await conn.fetch("""
            SELECT command, total_ms, state, created_at
            FROM request_traces
            WHERE created_at > NOW() - INTERVAL '1 minute' * $1
            ORDER BY total_ms DESC
        """, minutes)

    if not rows:
        return None

    red_items = []
    for r in rows:
        cat = classify_command(r['command'])
        _, yellow_max = THRESHOLDS.get(cat, THRESHOLDS['nav'])
        if r['total_ms'] > yellow_max:
            red_items.append(r)

    if not red_items:
        return None

    lines = [f"\U0001f6a8 <b>Latency Alert</b> ({len(red_items)} red in {minutes}min)\n"]
    for r in red_items[:5]:
        cat = classify_command(r['command'])
        ms = int(r['total_ms'])
        lines.append(f"  \U0001f534 {r['command']}: <b>{ms}ms</b> ({cat})")

    lines.append(f"\n\U0001f449 /latency for full report")
    return "\n".join(lines)
