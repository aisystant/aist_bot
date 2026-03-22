"""
DB-запросы для nudge-системы (WP-85, Phase 5C).

Таблица nudge_log хранит историю отправленных nudges для cooldown-проверки.

DEPRECATED (WP-152): nudge_log заменяется notification_log.
Двойная запись на переходный период. После стабилизации (W15+):
удалить nudge_log, was_nudge_sent_recently, log_nudge_sent.
"""

import logging
from datetime import datetime, timezone

from db.connection import get_pool

logger = logging.getLogger(__name__)


async def ensure_nudge_tables():
    """Создать таблицу nudge_log (idempotent)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS nudge_log (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                rule_id TEXT NOT NULL,
                nudge_key TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
                UNIQUE(chat_id, nudge_key)
            )
        ''')


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


async def was_nudge_sent_recently(chat_id: int, nudge_key: str, cooldown_days: int) -> bool:
    """Проверить, был ли nudge отправлен в пределах cooldown."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT sent_at FROM nudge_log
            WHERE chat_id = $1 AND nudge_key = $2
        ''', chat_id, nudge_key)

        if not row:
            return False

        sent_at = row['sent_at']
        days_ago = (datetime.utcnow() - sent_at).days
        return days_ago < cooldown_days


async def log_nudge_sent(chat_id: int, rule_id: str, nudge_key: str):
    """Записать факт отправки nudge (upsert для cooldown)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO nudge_log (chat_id, rule_id, nudge_key, sent_at)
            VALUES ($1, $2, $3, NOW() AT TIME ZONE 'utc')
            ON CONFLICT (chat_id, nudge_key) DO UPDATE
            SET sent_at = NOW() AT TIME ZONE 'utc', rule_id = $2
        ''', chat_id, rule_id, nudge_key)
