"""
Запросы для таблицы error_logs (мониторинг ошибок).

Паттерн аналогичен traces.py:
- check_error_alerts() → HTML-алерт для TG (scheduler каждые 15 мин)
- get_error_report() → полный отчёт для /errors
- cleanup_old_errors() → midnight cleanup
"""

from typing import Optional
from db.connection import acquire
from config import get_logger

logger = get_logger(__name__)


async def check_error_alerts(minutes: int = 15) -> Optional[str]:
    """Check for new (un-alerted) errors in the last N minutes.

    Returns HTML alert text if new errors found, None otherwise.
    Marks checked errors as alerted=True.
    """
    async with await acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, level, logger_name, message,
                   context, occurrence_count, last_seen_at
            FROM error_logs
            WHERE alerted = FALSE
              AND last_seen_at > NOW() - INTERVAL '1 minute' * $1
            ORDER BY last_seen_at DESC
            LIMIT 10
        """, minutes)

    if not rows:
        return None

    total_occurrences = sum(r['occurrence_count'] for r in rows)

    lines = [
        f"\u26a0\ufe0f <b>Error Alert</b> "
        f"({len(rows)} errors, {total_occurrences} occurrences in {minutes}min)\n"
    ]
    for r in rows:
        emoji = "\U0001f534" if r['level'] == 'CRITICAL' else "\U0001f7e1"
        msg = (r['message'] or '')[:80]
        ctx = r.get('context') or {}
        ctx_str = f" | user={ctx['user_id']}" if isinstance(ctx, dict) and ctx.get('user_id') else ""
        count_str = f" x{r['occurrence_count']}" if r['occurrence_count'] > 1 else ""
        lines.append(f"  {emoji} {r['logger_name']}: {msg}{count_str}{ctx_str}")

    lines.append(f"\n\U0001f449 /errors for full report")

    # Mark as alerted
    ids = [r['id'] for r in rows]
    async with await acquire() as conn:
        await conn.execute(
            "UPDATE error_logs SET alerted = TRUE WHERE id = ANY($1::int[])", ids
        )

    return "\n".join(lines)


async def get_error_report(hours: int = 24) -> dict:
    """Get error report for /errors dev command.

    Returns dict with:
      - summary: {unique_errors, total_occurrences, critical_count}
      - recent: [{level, logger_name, message, occurrence_count, last_seen_at, context}]
      - by_logger: [{logger_name, count, total_occurrences}]
    """
    async with await acquire() as conn:
        summary = await conn.fetchrow("""
            SELECT COUNT(*) AS unique_errors,
                   COALESCE(SUM(occurrence_count), 0)::int AS total_occurrences,
                   COUNT(*) FILTER (WHERE level = 'CRITICAL') AS critical_count
            FROM error_logs
            WHERE last_seen_at > NOW() - INTERVAL '1 hour' * $1
        """, hours)

        recent = await conn.fetch("""
            SELECT level, logger_name, message, occurrence_count,
                   last_seen_at, context
            FROM error_logs
            WHERE last_seen_at > NOW() - INTERVAL '1 hour' * $1
            ORDER BY last_seen_at DESC
            LIMIT 10
        """, hours)

        by_logger = await conn.fetch("""
            SELECT logger_name,
                   COUNT(*) AS count,
                   SUM(occurrence_count)::int AS total_occurrences
            FROM error_logs
            WHERE last_seen_at > NOW() - INTERVAL '1 hour' * $1
            GROUP BY logger_name
            ORDER BY total_occurrences DESC
        """, hours)

    return {
        'summary': dict(summary) if summary else {
            'unique_errors': 0, 'total_occurrences': 0, 'critical_count': 0
        },
        'recent': [dict(r) for r in recent],
        'by_logger': [dict(r) for r in by_logger],
    }


async def cleanup_old_errors(days: int = 7) -> int:
    """Delete error_logs older than N days."""
    async with await acquire() as conn:
        result = await conn.execute(
            "DELETE FROM error_logs WHERE last_seen_at < NOW() - INTERVAL '1 day' * $1",
            days,
        )
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"[Errors] Cleaned up {count} error logs older than {days} days")
        return count
