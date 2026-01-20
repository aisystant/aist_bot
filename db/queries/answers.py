"""
Запросы для работы с ответами и рабочими продуктами.
"""

import json
from datetime import date, timedelta
from typing import List, Optional

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def save_answer(chat_id: int, topic_index: int, answer: str,
                      mode: str = 'marathon', answer_type: str = 'theory_answer',
                      topic_id: str = None, work_product_category: str = None,
                      complexity_level: int = None, feed_session_id: int = None):
    """Сохранить ответ пользователя"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO answers 
            (chat_id, topic_index, answer, mode, answer_type, topic_id, 
             work_product_category, complexity_level, feed_session_id) 
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ''', chat_id, topic_index, answer, mode, answer_type, topic_id,
            work_product_category, complexity_level, feed_session_id)


async def get_answers(chat_id: int, limit: int = 100) -> List[dict]:
    """Получить ответы пользователя"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT * FROM answers 
            WHERE chat_id = $1 
            ORDER BY created_at DESC 
            LIMIT $2
        ''', chat_id, limit)
        
        return [dict(row) for row in rows]


async def get_weekly_work_products(chat_id: int, week_offset: int = 0) -> List[dict]:
    """
    Получить рабочие продукты за неделю.

    Args:
        chat_id: ID пользователя
        week_offset: 0 = текущая неделя, -1 = прошлая неделя

    Returns:
        Список рабочих продуктов
    """
    from .users import moscow_today

    today = moscow_today()
    # Начало недели (понедельник)
    week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    week_end = week_start + timedelta(days=7)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT 
                id, chat_id, mode, answer_type, work_product_category,
                answer, topic_index, topic_id, feed_session_id, created_at
            FROM answers
            WHERE chat_id = $1
              AND answer_type IN ('work_product', 'fixation')
              AND created_at >= $2
              AND created_at < $3
            ORDER BY created_at DESC
        ''', chat_id, week_start, week_end)
        
        return [dict(row) for row in rows]


async def get_answers_count_by_type(chat_id: int) -> dict:
    """Получить количество ответов по типам"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT answer_type, COUNT(*) as count
            FROM answers
            WHERE chat_id = $1
            GROUP BY answer_type
        ''', chat_id)
        
        return {row['answer_type']: row['count'] for row in rows}
