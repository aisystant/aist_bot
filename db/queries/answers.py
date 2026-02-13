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


async def delete_marathon_answers(chat_id: int) -> int:
    """
    Удалить все ответы марафона для пользователя.
    Вызывается при сбросе марафона.

    Returns:
        Количество удалённых записей
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('''
            DELETE FROM answers
            WHERE chat_id = $1 AND mode = 'marathon'
        ''', chat_id)
        # result format: "DELETE N"
        deleted_count = int(result.split()[-1]) if result else 0
        logger.info(f"Deleted {deleted_count} marathon answers for chat_id={chat_id}")
        return deleted_count


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


async def get_work_products_by_day(chat_id: int, topics_list: list) -> dict:
    """
    Получить количество рабочих продуктов по дням марафона.

    Считает УНИКАЛЬНЫЕ topic_index, чтобы избежать дублирования
    при повторной отправке РП за одно задание.

    Args:
        chat_id: ID пользователя
        topics_list: список тем с полем 'day'

    Returns:
        {day_number: work_products_count}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Считаем уникальные topic_index для каждого РП
        rows = await conn.fetch('''
            SELECT DISTINCT topic_index
            FROM answers
            WHERE chat_id = $1
              AND answer_type = 'work_product'
        ''', chat_id)

    # Группируем по дням
    wp_by_day = {}
    for row in rows:
        topic_idx = row['topic_index']
        if topic_idx < len(topics_list):
            day = topics_list[topic_idx].get('day', 1)
            wp_by_day[day] = wp_by_day.get(day, 0) + 1

    return wp_by_day


async def get_weekly_marathon_stats(chat_id: int) -> dict:
    """
    Получить статистику марафона за текущую неделю.

    Returns:
        {
            'active_days': int,
            'theory_answers': int,   # Ответы на уроки (уникальные topic_index)
            'work_products': int,    # Рабочие продукты (уникальные topic_index)
            'bonus_answers': int     # Бонусные ответы
        }
    """
    from .users import moscow_today

    today = moscow_today()
    week_start = today - timedelta(days=today.weekday())  # Понедельник

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Активные дни за неделю (марафон)
        active_days = await conn.fetchval('''
            SELECT COUNT(DISTINCT activity_date)
            FROM activity_log
            WHERE chat_id = $1
              AND mode = 'marathon'
              AND activity_date >= $2
        ''', chat_id, week_start)

        # Рабочие продукты за неделю (уникальные topic_index)
        work_products = await conn.fetchval('''
            SELECT COUNT(DISTINCT topic_index)
            FROM answers
            WHERE chat_id = $1
              AND mode = 'marathon'
              AND created_at >= $2
              AND answer_type = 'work_product'
        ''', chat_id, week_start)

        # Ответы на уроки за неделю (уникальные topic_index)
        theory_answers = await conn.fetchval('''
            SELECT COUNT(DISTINCT topic_index)
            FROM answers
            WHERE chat_id = $1
              AND mode = 'marathon'
              AND created_at >= $2
              AND answer_type = 'theory_answer'
        ''', chat_id, week_start)

        # Бонусные ответы за неделю
        bonus_answers = await conn.fetchval('''
            SELECT COUNT(*)
            FROM answers
            WHERE chat_id = $1
              AND mode = 'marathon'
              AND created_at >= $2
              AND answer_type = 'bonus_answer'
        ''', chat_id, week_start)

    return {
        'active_days': active_days or 0,
        'theory_answers': theory_answers or 0,
        'work_products': work_products or 0,
        'bonus_answers': bonus_answers or 0,
    }


async def get_weekly_feed_stats(chat_id: int) -> dict:
    """
    Получить статистику ленты за текущую неделю.

    Дайджесты = все сессии (показаны пользователю), по session_date.
    Фиксации = завершённые сессии (пользователь написал фиксацию), по session_date.
    Инвариант: digests >= fixations.

    Returns:
        {
            'active_days': int,
            'digests': int,
            'fixations': int
        }
    """
    from .users import moscow_today

    today = moscow_today()
    week_start = today - timedelta(days=today.weekday())

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Активные дни за неделю (лента)
        active_days = await conn.fetchval('''
            SELECT COUNT(DISTINCT activity_date)
            FROM activity_log
            WHERE chat_id = $1
              AND mode = 'feed'
              AND activity_date >= $2
        ''', chat_id, week_start)

        # Дайджесты за неделю (все сессии по session_date)
        digests = await conn.fetchval('''
            SELECT COUNT(*)
            FROM feed_sessions
            WHERE week_id IN (
                SELECT id FROM feed_weeks WHERE chat_id = $1
            )
            AND session_date >= $2
        ''', chat_id, week_start)

        # Фиксации за неделю (завершённые сессии по session_date)
        fixations = await conn.fetchval('''
            SELECT COUNT(*)
            FROM feed_sessions
            WHERE week_id IN (
                SELECT id FROM feed_weeks WHERE chat_id = $1
            )
            AND status = 'completed'
            AND session_date >= $2
        ''', chat_id, week_start)

    return {
        'active_days': active_days or 0,
        'digests': digests or 0,
        'fixations': fixations or 0
    }


async def get_total_stats(chat_id: int) -> dict:
    """
    Получить общую статистику с даты регистрации (или с даты сброса).

    Returns:
        {
            'registered_at': date,
            'days_since_start': int,
            'total_active_days': int,
            'total_work_products': int,
            'total_digests': int,
            'total_fixations': int,
            'stats_reset_date': date | None
        }
    """
    from .users import get_intern, moscow_today

    user = await get_intern(chat_id)
    today = moscow_today()
    stats_reset_date = user.get('stats_reset_date')

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Определяем дату регистрации
        created_at = user.get('created_at')
        if created_at:
            if hasattr(created_at, 'date'):
                start_date = created_at.date()
            else:
                start_date = created_at
        else:
            first_activity = await conn.fetchval('''
                SELECT MIN(activity_date) FROM activity_log WHERE chat_id = $1
            ''', chat_id)
            if first_activity:
                start_date = first_activity
            else:
                first_answer = await conn.fetchval('''
                    SELECT MIN(created_at)::date FROM answers WHERE chat_id = $1
                ''', chat_id)
                start_date = first_answer if first_answer else today

        # Если был сброс — считаем от даты сброса
        count_from = stats_reset_date if stats_reset_date else start_date
        days_since_start = (today - count_from).days + 1

        # Всего активных дней (от даты сброса)
        total_active_days = await conn.fetchval('''
            SELECT COUNT(DISTINCT activity_date)
            FROM activity_log
            WHERE chat_id = $1 AND activity_date >= $2
        ''', chat_id, count_from)

        # Всего рабочих продуктов (от даты сброса)
        work_products = await conn.fetchval('''
            SELECT COUNT(DISTINCT topic_index)
            FROM answers
            WHERE chat_id = $1
              AND answer_type = 'work_product'
              AND created_at >= $2
        ''', chat_id, count_from)

        # Всего дайджестов (от даты сброса, все сессии по session_date)
        digests = await conn.fetchval('''
            SELECT COUNT(*)
            FROM feed_sessions
            WHERE week_id IN (
                SELECT id FROM feed_weeks WHERE chat_id = $1
            )
            AND session_date >= $2
        ''', chat_id, count_from)

        # Всего фиксаций (от даты сброса, завершённые сессии по session_date)
        fixations = await conn.fetchval('''
            SELECT COUNT(*)
            FROM feed_sessions
            WHERE week_id IN (
                SELECT id FROM feed_weeks WHERE chat_id = $1
            )
            AND status = 'completed'
            AND session_date >= $2
        ''', chat_id, count_from)

    return {
        'registered_at': start_date,
        'days_since_start': days_since_start,
        'total_active_days': total_active_days or 0,
        'total_work_products': work_products or 0,
        'total_digests': digests or 0,
        'total_fixations': fixations or 0,
        'stats_reset_date': stats_reset_date,
    }


async def reset_user_stats(chat_id: int) -> None:
    """
    Сбросить статистику пользователя.
    Сохраняет active_days_total (общее количество активных дней).
    Сбрасывает: streak, longest_streak, last_active_date.
    Устанавливает stats_reset_date = сегодня.
    """
    from .users import update_intern, moscow_today

    today = moscow_today()
    await update_intern(chat_id,
        stats_reset_date=today,
        active_days_streak=0,
        longest_streak=0,
        last_active_date=None,
    )
