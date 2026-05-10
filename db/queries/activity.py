from __future__ import annotations

"""
Запросы для отслеживания активности и систематичности.
"""

from datetime import date, timedelta
from typing import List, Optional

from config import get_logger
from db.connection import get_pool, get_learning_pool

logger = get_logger(__name__)


async def touch_last_active_date(chat_id: int):
    """Обновить last_active_date если ещё не сегодня. 1 lightweight UPDATE.

    Вызывается fire-and-forget из TracingMiddleware на КАЖДЫЙ запрос.
    Условие WHERE гарантирует: max 1 реальный UPDATE/день на пользователя.
    """
    from .users import moscow_today

    pool = await get_pool()
    today = moscow_today()
    async with pool.acquire() as conn:
        await conn.execute('''
            UPDATE development.user_state
            SET last_active_date = $2
            WHERE chat_id = $1
              AND (last_active_date IS NULL OR last_active_date < $2)
        ''', chat_id, today)


async def record_active_day(chat_id: int, activity_type: str,
                           mode: str = 'marathon', reference_id: int = None):
    """
    Записать активный день.

    Вызывается при любом текстовом ответе:
    - theory_answer, work_product, bonus_answer (марафон)
    - feed_fixation (лента)
    - question_asked (вопросы)

    Args:
        chat_id: ID пользователя
        activity_type: тип активности
        mode: режим (marathon/feed)
        reference_id: ID связанной записи (answers.id или feed_sessions.id)
    """
    from .users import get_intern, update_intern, moscow_today

    pool = await get_learning_pool()
    today = moscow_today()

    # 1. Записать в лог активности
    async with pool.acquire() as conn:
        try:
            await conn.execute('''
                INSERT INTO activity_log (chat_id, activity_date, activity_type, mode, reference_id)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (chat_id, activity_date, activity_type) DO NOTHING
            ''', chat_id, today, activity_type, mode, reference_id)
        except Exception as e:
            logger.warning(f"Не удалось записать активность: {e}")

    # 2. Обновить счётчики пользователя
    user = await get_intern(chat_id)
    last_active = user.get('last_active_date')

    # Уже был активен сегодня — ничего не делаем
    if last_active == today:
        return

    # Считаем streak
    if last_active == today - timedelta(days=1):
        # Продолжаем серию
        new_streak = user['active_days_streak'] + 1
    else:
        # Серия прервалась
        new_streak = 1

    # Обновляем рекорд
    longest = max(user.get('longest_streak', 0), new_streak)

    await update_intern(chat_id,
        active_days_total=user['active_days_total'] + 1,
        active_days_streak=new_streak,
        longest_streak=longest,
        last_active_date=today
    )

    logger.info(f"📅 Активный день для {chat_id}: streak={new_streak}, total={user['active_days_total'] + 1}")


async def get_activity_stats(chat_id: int) -> dict:
    """Получить статистику активности пользователя"""
    from .users import get_intern, moscow_today

    pool = await get_learning_pool()
    user = await get_intern(chat_id)
    today = moscow_today()

    # Активность с понедельника текущей недели
    week_start = today - timedelta(days=today.weekday())

    async with pool.acquire() as conn:
        recent_activity = await conn.fetch('''
            SELECT activity_date, activity_type, mode
            FROM activity_log
            WHERE chat_id = $1 AND activity_date >= $2
            ORDER BY activity_date DESC
        ''', chat_id, week_start)

    # Количество активных дней с понедельника текущей недели
    days_active_this_week = len(set(a['activity_date'] for a in recent_activity))

    return {
        'total': user['active_days_total'],
        'streak': user['active_days_streak'],
        'longest_streak': user['longest_streak'],
        'last_active': user['last_active_date'],
        'days_active_this_week': days_active_this_week,
        'recent_activity': [dict(a) for a in recent_activity]
    }


async def record_service_usage(user_id: int, service_id: str, action: str = "enter") -> None:
    """Записать использование сервиса для аналитики.

    Данные: user_id, service_id, action, timestamp.
    Используется для адаптивной сортировки меню.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO service_usage (user_id, service_id, action)
            VALUES ($1, $2, $3)
        ''', user_id, service_id, action)


async def get_service_usage_counts(user_id: int) -> dict[str, int]:
    """Получить количество использований каждого сервиса.

    Returns:
        Dict: {service_id: count}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT service_id, COUNT(*) as cnt
            FROM service_usage
            WHERE user_id = $1
            GROUP BY service_id
            ORDER BY cnt DESC
        ''', user_id)
    return {row['service_id']: row['cnt'] for row in rows}


async def get_activity_calendar(chat_id: int, weeks: int = 4) -> List[dict]:
    """
    Получить календарь активности за последние N недель.

    Returns:
        Список дней с информацией об активности
    """
    from .users import moscow_today

    pool = await get_learning_pool()
    today = moscow_today()
    start_date = today - timedelta(weeks=weeks)

    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT DISTINCT activity_date
            FROM activity_log
            WHERE chat_id = $1 AND activity_date >= $2
            ORDER BY activity_date
        ''', chat_id, start_date)

    active_dates = {row['activity_date'] for row in rows}

    # Генерируем календарь
    calendar = []
    current = start_date
    while current <= today:
        calendar.append({
            'date': current,
            'weekday': current.weekday(),  # 0=Пн, 6=Вс
            'active': current in active_dates,
            'is_future': current > today
        })
        current += timedelta(days=1)

    return calendar
