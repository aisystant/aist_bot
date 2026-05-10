from __future__ import annotations

"""
DB-запросы для nudge-системы (WP-85, Phase 5C).

Cooldown и dedup перенесены в notification_log (WP-152).
Этот модуль содержит только получение кандидатов.
"""

import logging

from db.connection import get_pool

logger = logging.getLogger(__name__)


async def get_nudge_candidates() -> list[dict]:
    """Получить пользователей T1+ с engagement + derived данными для nudge-анализа.

    WP-117 Ф2+: расширено с T3+ до T1+ (все с привязанным аккаунтом).
    Добавлен 3_derived для stage-aware nudges.

    Returns:
        List of dicts with user_meta + engagement + derived data.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT
                s.chat_id,
                u.language,
                u.name,
                s.last_active_date,
                s.active_days_total,
                s.active_days_streak,
                s.longest_streak,
                s.marathon_status,
                s.bot_blocked,
                s.notify_nudges,
                u.tier,
                dt.data->'2_collected' AS engagement,
                dt.data->'3_derived' AS derived
            FROM development.user_state s
            JOIN public.users u ON u.telegram_id = s.chat_id
            LEFT JOIN digital_twins dt ON dt.user_id = u.id::text
            WHERE u.tier NOT IN ('T0')
              AND s.bot_blocked = FALSE
              AND COALESCE(s.notify_nudges, TRUE) = TRUE
        ''')
        return [dict(r) for r in rows]


