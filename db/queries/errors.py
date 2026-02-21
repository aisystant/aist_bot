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

    Enhanced with classifier data: category, severity, suggested_action (WP-45).
    Returns HTML alert text if new errors found, None otherwise.
    Marks checked errors as alerted=True.
    """
    _SEV_EMOJI = {"L4": "\U0001f534", "L3": "\U0001f7e0", "L2": "\U0001f7e1", "L1": "\U0001f7e2"}

    async with await acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, level, logger_name, message,
                   context, occurrence_count, last_seen_at,
                   category, severity, suggested_action
            FROM error_logs
            WHERE alerted = FALSE
              AND last_seen_at > NOW() - INTERVAL '1 minute' * $1
            ORDER BY
                CASE severity
                    WHEN 'L4' THEN 1 WHEN 'L3' THEN 2
                    WHEN 'L2' THEN 3 WHEN 'L1' THEN 4
                    ELSE 5
                END,
                last_seen_at DESC
            LIMIT 10
        """, minutes)

    if not rows:
        return None

    total_occurrences = sum(r['occurrence_count'] for r in rows)

    lines = [
        f"\u26a0\ufe0f <b>\u041e\u0448\u0438\u0431\u043a\u0438</b> "
        f"({len(rows)} \u043e\u0448\u0438\u0431\u043e\u043a, {total_occurrences} \u0441\u043b\u0443\u0447\u0430\u0435\u0432 \u0437\u0430 {minutes} \u043c\u0438\u043d)\n"
    ]
    for r in rows:
        sev = r.get('severity') or '??'
        cat = r.get('category') or '?'
        emoji = _SEV_EMOJI.get(r.get('severity'), "\u26aa")
        msg = (r['message'] or '')[:60]
        count_str = f" x{r['occurrence_count']}" if r['occurrence_count'] > 1 else ""
        action = r.get('suggested_action')
        action_str = f"\n    \U0001f4a1 {action}" if action else ""
        lines.append(f"  {emoji} [{cat}/{sev}] {msg}{count_str}{action_str}")

    lines.append(f"\n\U0001f449 /errors \u2014 \u043f\u043e\u043b\u043d\u044b\u0439 \u043e\u0442\u0447\u0451\u0442")

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
