"""
Агрегированный профиль знаний пользователя.

Использует VIEW user_knowledge_profile (db/models.py).
"""

from typing import Optional

from db import get_pool
from config import get_logger

logger = get_logger(__name__)


async def get_knowledge_profile(chat_id: int) -> Optional[dict]:
    """Агрегированный профиль знаний пользователя.

    Возвращает данные из VIEW user_knowledge_profile:
    - Профиль (name, occupation, role, domain, interests, goals)
    - Состояние обучения (marathon_status, feed_status, complexity_level)
    - Систематичность (active_days_total, streak, longest_streak)
    - Агрегаты (theory_answers_count, work_products_count, qa_count,
      total_digests, total_fixations, current_feed_topics)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM user_knowledge_profile WHERE chat_id = $1',
            chat_id
        )
        return dict(row) if row else None
