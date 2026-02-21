"""
Запросы для конверсионных событий (DP.ARCH.002 § 12.8).

Таблица conversion_events:
- trigger_type: C1-C7 (тип триггера)
- milestone: day_7, day_14, day_30, day_60, day_90
- action: shown / clicked / dismissed
"""

from datetime import datetime, timedelta
from typing import Optional

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)

# Cooldown: 7 дней между предложениями (§ 12.8)
COOLDOWN_DAYS = 7

# Milestones (§ 12.5)
MILESTONE_DAYS = [7, 14, 30, 60, 90]


async def log_conversion_event(
    chat_id: int,
    trigger_type: str,
    milestone: str = None,
    action: str = "shown",
) -> None:
    """Записать конверсионное событие."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO conversion_events
               (chat_id, trigger_type, milestone, action)
               VALUES ($1, $2, $3, $4)''',
            chat_id, trigger_type, milestone, action,
        )


async def was_milestone_sent(chat_id: int, milestone: str, trigger_type: str = None) -> bool:
    """Проверить, было ли уведомление уже отправлено.

    Args:
        trigger_type: если None — ищет по любому trigger_type.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if trigger_type:
            row = await conn.fetchval(
                '''SELECT 1 FROM conversion_events
                   WHERE chat_id = $1
                     AND trigger_type = $2
                     AND milestone = $3
                   LIMIT 1''',
                chat_id, trigger_type, milestone,
            )
        else:
            row = await conn.fetchval(
                '''SELECT 1 FROM conversion_events
                   WHERE chat_id = $1
                     AND milestone = $2
                   LIMIT 1''',
                chat_id, milestone,
            )
        return row is not None


async def is_cooldown_active(chat_id: int) -> bool:
    """Проверить, активен ли cooldown (7 дней с последнего предложения)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        last = await conn.fetchval(
            '''SELECT MAX(shown_at) FROM conversion_events
               WHERE chat_id = $1''',
            chat_id,
        )
        if last is None:
            return False
        # Timezone-naive comparison
        if last.tzinfo:
            from datetime import timezone
            now = datetime.now(timezone.utc)
        else:
            now = datetime.utcnow()
        return (now - last) < timedelta(days=COOLDOWN_DAYS)


async def get_milestone_eligible_users(milestone_day: int) -> list[dict]:
    """Найти пользователей, достигших milestone (по created_at).

    Возвращает пользователей:
    - created_at ровно milestone_day дней назад (±1 день)
    - onboarding_completed = TRUE
    - НЕ получали C3 milestone для этого дня
    - НЕ в cooldown (нет conversion_events за последние 7 дней)

    Returns:
        Список dict с chat_id, language, mode, completed_topics и т.д.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        milestone = f"day_{milestone_day}"
        rows = await conn.fetch(
            '''SELECT i.chat_id, i.language, i.mode,
                      i.completed_topics, i.marathon_status,
                      i.active_days_total, i.longest_streak,
                      i.complexity_level, i.feed_status
               FROM interns i
               WHERE i.onboarding_completed = TRUE
                 AND i.created_at IS NOT NULL
                 AND i.created_at + INTERVAL '1 day' * $1 <= NOW()
                 AND i.created_at + INTERVAL '1 day' * ($1 + 1) > NOW()
                 AND i.chat_id NOT IN (
                     SELECT ce.chat_id FROM conversion_events ce
                     WHERE ce.trigger_type = 'C3'
                       AND ce.milestone = $2
                 )
                 AND i.chat_id NOT IN (
                     SELECT ce2.chat_id FROM conversion_events ce2
                     WHERE ce2.shown_at > NOW() - INTERVAL '7 days'
                 )''',
            milestone_day, milestone,
        )
        return [dict(r) for r in rows]
