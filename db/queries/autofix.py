"""
Запросы для таблицы pending_fixes (L2 Auto-Fix, WP-45 Phase 3).

Паттерн аналогичен errors.py:
- get_l2_fixable_errors() → ошибки, подходящие для авто-исправления
- create_pending_fix() → создать запись предложения
- get_pending_fix() → получить по ID
- update_fix_status() → обновить статус (approved/rejected/applied/failed)
- cleanup_old_fixes() → cleanup (midnight, 30 дней)
"""

from typing import Optional
from db.connection import acquire
from config import get_logger

logger = get_logger(__name__)


async def get_l2_fixable_errors(
    minutes: int = 15, min_count: int = 3, limit: int = 3
) -> list[dict]:
    """Find L2 errors eligible for auto-fix.

    Criteria:
    - severity = 'L2'
    - occurrence_count >= min_count
    - last_seen_at within last N minutes
    - not already in pending_fixes (status in pending/approved/applied)
    """
    async with await acquire() as conn:
        rows = await conn.fetch("""
            SELECT e.id, e.error_key, e.category, e.severity,
                   e.logger_name, e.message, e.traceback, e.context,
                   e.occurrence_count, e.suggested_action
            FROM error_logs e
            LEFT JOIN pending_fixes pf
                ON e.error_key = pf.error_key
                AND pf.status IN ('pending', 'approved', 'applied')
            WHERE e.severity = 'L2'
              AND e.occurrence_count >= $1
              AND e.last_seen_at > NOW() - INTERVAL '1 minute' * $2
              AND pf.id IS NULL
            ORDER BY e.occurrence_count DESC
            LIMIT $3
        """, min_count, minutes, limit)

        return [dict(r) for r in rows]


async def create_pending_fix(
    error_log_id: int,
    error_key: str,
    diagnosis: str,
    archgate_eval: str,
    proposed_diff: str,
    file_path: str,
    tg_message_id: int | None = None,
) -> Optional[int]:
    """Create a pending fix record. Returns fix ID."""
    async with await acquire() as conn:
        try:
            row = await conn.fetchrow("""
                INSERT INTO pending_fixes
                    (error_log_id, error_key, diagnosis, archgate_eval,
                     proposed_diff, file_path, tg_message_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
            """, error_log_id, error_key, diagnosis, archgate_eval,
                proposed_diff, file_path, tg_message_id)
            return row['id'] if row else None
        except Exception as e:
            # Unique constraint violation = already proposed
            logger.warning(f"[AutoFix] create_pending_fix failed: {e}")
            return None


async def get_pending_fix(fix_id: int) -> Optional[dict]:
    """Get pending fix by ID."""
    async with await acquire() as conn:
        row = await conn.fetchrow("""
            SELECT * FROM pending_fixes WHERE id = $1
        """, fix_id)
        return dict(row) if row else None


async def update_fix_status(
    fix_id: int,
    status: str,
    pr_url: str | None = None,
    branch_name: str | None = None,
) -> None:
    """Update fix status and optional PR info."""
    async with await acquire() as conn:
        await conn.execute("""
            UPDATE pending_fixes
            SET status = $2,
                pr_url = COALESCE($3, pr_url),
                branch_name = COALESCE($4, branch_name),
                resolved_at = CASE WHEN $2 IN ('applied', 'rejected', 'failed')
                              THEN NOW() ELSE resolved_at END
            WHERE id = $1
        """, fix_id, status, pr_url, branch_name)


async def cleanup_old_fixes(days: int = 30) -> int:
    """Delete old resolved fixes."""
    async with await acquire() as conn:
        result = await conn.execute("""
            DELETE FROM pending_fixes
            WHERE status IN ('applied', 'rejected', 'failed')
              AND resolved_at < NOW() - INTERVAL '1 day' * $1
        """, days)
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"[AutoFix] Cleaned up {count} old fixes")
        return count
