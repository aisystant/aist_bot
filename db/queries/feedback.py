"""
Запросы для обратной связи.

WP-253 lift-and-shift (8 мая 2026): таблица переехала в Neon journal БД.
- feedback_reports → journal.feedback_report (journal pool)

NOTE: LEFT JOIN на public.users (legacy bot_data) выполняется в Python через
отдельный get_pool()-запрос. TODO: миграция users → persona — делает главный
агент; после этого enrichment имени переедет в persona pool.
"""

from typing import List, Optional

from config import get_logger
from db.connection import get_journal_pool, get_pool

logger = get_logger(__name__)


def format_user_label(report: dict) -> str:
    """Форматирует имя отправителя: 'Имя (@username)' или fallback на chat_id."""
    name = report.get('user_name') or ''
    tg = report.get('tg_username') or ''
    cid = report.get('chat_id', '?')
    if name and tg:
        return f"{name} (@{tg})"
    if tg:
        return f"@{tg}"
    if name:
        return f"{name} (#{cid})"
    return f"#{cid}"


async def _enrich_user_fields(reports: List[dict]) -> List[dict]:
    """Добавить user_name + tg_username из legacy public.users.

    TODO (WP-253): после миграции users → persona заменить get_pool на
    get_persona_pool и таблицу public.users на persona.ory_identity.
    """
    if not reports:
        return reports
    chat_ids = list({r["chat_id"] for r in reports if r.get("chat_id") is not None})
    if not chat_ids:
        for r in reports:
            r.setdefault("user_name", None)
            r.setdefault("tg_username", None)
        return reports
    legacy_pool = await get_pool()
    async with legacy_pool.acquire() as conn:
        users = await conn.fetch(
            '''SELECT telegram_id, name AS user_name, tg_username
               FROM public.users
               WHERE telegram_id = ANY($1::bigint[])''',
            chat_ids,
        )
    user_by_chat = {u["telegram_id"]: u for u in users}
    for r in reports:
        u = user_by_chat.get(r.get("chat_id"))
        if u:
            r["user_name"] = u["user_name"]
            r["tg_username"] = u["tg_username"]
        else:
            r.setdefault("user_name", None)
            r.setdefault("tg_username", None)
    return reports


async def save_feedback(
    chat_id: int,
    category: str,
    scenario: str,
    severity: str,
    message: str,
) -> Optional[int]:
    """Сохранить отчёт. Возвращает id записи."""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO feedback_report
            (chat_id, category, scenario, severity, message)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        ''', chat_id, category, scenario, severity, message)
        return row['id'] if row else None


async def get_pending_reports(severity: str, since_hours: int = 24) -> List[dict]:
    """Получить неотправленные отчёты по severity за N часов."""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT f.id, f.chat_id, f.category, f.scenario, f.severity,
                   f.message, f.created_at
            FROM feedback_report f
            WHERE f.status = 'new' AND f.severity = $1
              AND f.created_at >= NOW() - make_interval(hours => $2)
            ORDER BY f.created_at
        ''', severity, since_hours)
        reports = [dict(r) for r in rows]
    return await _enrich_user_fields(reports)


async def mark_notified(ids: List[int]):
    """Пометить отчёты как отправленные."""
    if not ids:
        return
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE feedback_report SET status = 'notified', notified_at = NOW() WHERE id = ANY($1)",
            ids,
        )


async def get_all_reports(limit: int = 20, since_hours: int = None) -> List[dict]:
    """Получить отчёты. since_hours=24 → за день, 168 → за неделю, None → все."""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        if since_hours:
            rows = await conn.fetch('''
                SELECT f.id, f.chat_id, f.category, f.scenario, f.severity,
                       f.message, f.status, f.created_at
                FROM feedback_report f
                WHERE f.created_at >= NOW() - make_interval(hours => $1)
                ORDER BY f.created_at DESC
                LIMIT $2
            ''', since_hours, limit)
        else:
            rows = await conn.fetch('''
                SELECT f.id, f.chat_id, f.category, f.scenario, f.severity,
                       f.message, f.status, f.created_at
                FROM feedback_report f
                ORDER BY f.created_at DESC
                LIMIT $1
            ''', limit)
        reports = [dict(r) for r in rows]
    return await _enrich_user_fields(reports)


async def get_report_stats() -> dict:
    """Статистика по отчётам."""
    pool = await get_journal_pool()
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
            FROM feedback_report
        ''')
        return dict(row) if row else {}


async def clear_all_reports() -> int:
    """Удалить все отчёты. Возвращает количество удалённых."""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('DELETE FROM feedback_report')
        # result = "DELETE N"
        return int(result.split()[-1]) if result else 0
