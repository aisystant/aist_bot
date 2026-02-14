"""
Запросы для обратной связи (feedback_reports).
"""

from typing import List, Optional

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def save_feedback(
    chat_id: int,
    category: str,
    scenario: str,
    severity: str,
    message: str,
) -> Optional[int]:
    """Сохранить отчёт. Возвращает id записи."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO feedback_reports
            (chat_id, category, scenario, severity, message)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        ''', chat_id, category, scenario, severity, message)
        return row['id'] if row else None


async def get_pending_reports(severity: str, since_hours: int = 24) -> List[dict]:
    """Получить неотправленные отчёты по severity за N часов."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT id, chat_id, category, scenario, severity, message, created_at
            FROM feedback_reports
            WHERE status = 'new' AND severity = $1
              AND created_at >= NOW() - make_interval(hours => $2)
            ORDER BY created_at
        ''', severity, since_hours)
        return [dict(r) for r in rows]


async def mark_notified(ids: List[int]):
    """Пометить отчёты как отправленные."""
    if not ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE feedback_reports SET status = 'notified', notified_at = NOW() WHERE id = ANY($1)",
            ids,
        )


async def get_all_reports(limit: int = 20, status_filter: str = None) -> List[dict]:
    """Получить все отчёты (для /reports)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status_filter:
            rows = await conn.fetch('''
                SELECT id, chat_id, category, scenario, severity, message, status, created_at
                FROM feedback_reports
                WHERE status = $1
                ORDER BY created_at DESC
                LIMIT $2
            ''', status_filter, limit)
        else:
            rows = await conn.fetch('''
                SELECT id, chat_id, category, scenario, severity, message, status, created_at
                FROM feedback_reports
                ORDER BY created_at DESC
                LIMIT $1
            ''', limit)
        return [dict(r) for r in rows]


async def get_report_stats() -> dict:
    """Статистика по отчётам."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'new') AS new_count,
                COUNT(*) FILTER (WHERE status = 'notified') AS notified_count,
                COUNT(*) FILTER (WHERE status = 'resolved') AS resolved_count,
                COUNT(*) FILTER (WHERE severity = 'red') AS red_count,
                COUNT(*) FILTER (WHERE severity = 'yellow') AS yellow_count,
                COUNT(*) FILTER (WHERE severity = 'green') AS green_count
            FROM feedback_reports
        ''')
        return dict(row) if row else {}
