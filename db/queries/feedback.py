"""
Запросы для обратной связи (feedback_reports).
"""

from typing import List, Optional

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


def format_user_label(report: dict) -> str:
    """Форматирует имя отправителя: 'Имя (@username)' или fallback на chat_id."""
    name = report.get('user_name') or ''
    tg = report.get('tg_username') or ''
    if name and tg:
        return f"{name} (@{tg})"
    if tg:
        return f"@{tg}"
    if name:
        return name
    return f"#{report.get('chat_id', '?')}"


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
            SELECT f.id, f.chat_id, f.category, f.scenario, f.severity,
                   f.message, f.created_at,
                   i.name AS user_name, i.tg_username
            FROM feedback_reports f
            LEFT JOIN interns i ON i.chat_id = f.chat_id
            WHERE f.status = 'new' AND f.severity = $1
              AND f.created_at >= NOW() - make_interval(hours => $2)
            ORDER BY f.created_at
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


async def get_all_reports(limit: int = 20, since_hours: int = None) -> List[dict]:
    """Получить отчёты. since_hours=24 → за день, 168 → за неделю, None → все."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since_hours:
            rows = await conn.fetch('''
                SELECT f.id, f.chat_id, f.category, f.scenario, f.severity,
                       f.message, f.status, f.created_at,
                       i.name AS user_name, i.tg_username
                FROM feedback_reports f
                LEFT JOIN interns i ON i.chat_id = f.chat_id
                WHERE f.created_at >= NOW() - make_interval(hours => $1)
                ORDER BY f.created_at DESC
                LIMIT $2
            ''', since_hours, limit)
        else:
            rows = await conn.fetch('''
                SELECT f.id, f.chat_id, f.category, f.scenario, f.severity,
                       f.message, f.status, f.created_at,
                       i.name AS user_name, i.tg_username
                FROM feedback_reports f
                LEFT JOIN interns i ON i.chat_id = f.chat_id
                ORDER BY f.created_at DESC
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


async def clear_all_reports() -> int:
    """Удалить все отчёты. Возвращает количество удалённых."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('DELETE FROM feedback_reports')
        # result = "DELETE N"
        return int(result.split()[-1]) if result else 0
