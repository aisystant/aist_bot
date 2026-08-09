from __future__ import annotations

"""
Запросы для отслеживания активности и систематичности.
"""

from datetime import date, timedelta
from typing import List, Optional

from config import get_logger
from db.connection import get_pool, get_learning_pool

logger = get_logger(__name__)


def compute_streak_update(current_streak: int, had_yesterday: bool, current_longest: int) -> tuple[int, int]:
    """Новые (active_days_streak, longest_streak) для одного нового активного дня.

    active_days_streak > 0 обязателен в условии продолжения: без него пользователь
    после сброса (streak=0) с ещё не удалённым вчерашним daily_activity_marker
    получил бы продолжение чужой серии (WP-7 Ф48, найдено Codex в peer-review).
    """
    continues = current_streak > 0 and had_yesterday
    new_streak = current_streak + 1 if continues else 1
    new_longest = max(current_longest or 0, new_streak)
    return new_streak, new_longest


async def touch_last_active_date(chat_id: int):
    """Обновить last_active_date если ещё не сегодня. 1 lightweight UPDATE.

    Вызывается fire-and-forget из TracingMiddleware на КАЖДЫЙ запрос.
    Условие WHERE гарантирует: max 1 реальный UPDATE/день на пользователя.

    Observability (peer-session 2026-06-04-02): запускается через asyncio.create_task,
    а голый `except` в middleware ловит только ПЛАНИРОВАНИЕ задачи, не её исполнение.
    Поэтому исключения логируем здесь — иначе они теряются как
    "Task exception was never retrieved" и last_active_date молча не пишется без следа.
    """
    from .users import moscow_today

    today = moscow_today()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute('''
                UPDATE development.user_state
                SET last_active_date = $2
                WHERE chat_id = $1
                  AND (last_active_date IS NULL OR last_active_date < $2)
            ''', chat_id, today)
    except Exception as e:
        logger.warning(f"[touch_last_active_date] failed for chat_id={chat_id}: {e}")


async def record_active_day(chat_id: int, activity_type: str,
                           mode: str = 'marathon', reference_id: int = None):
    """
    Записать активный день.

    Вызывается при значимых действиях — известные production call sites
    (сверено живым grep, WP-7 Ф48, peer-session 2026-08-04-27-wp7-f48-bot-reliability):
    - theory_answer, work_product, bonus_answer (марафон, core/topics.py)
    - feed_fixation (лента, два независимых движка)
    - question_asked (вопросы)
    - marathon_checkin (handlers/marathon.py)

    Args:
        chat_id: ID пользователя
        activity_type: тип активности
        mode: режим (marathon/feed)
        reference_id: ID связанной записи (answers.id или feed_sessions.id)
    """
    from .users import moscow_today
    from db.queries.internal_metrics import record_internal_metric

    learning_pool = await get_learning_pool()
    dev_pool = await get_pool()
    today = moscow_today()

    # 1. Записать в лог активности (learning БД) — аналитика/аудит, НЕ гейт счётчика
    #    (см. п.2: гейт теперь атомарный и живёт в dev_pool).
    async with learning_pool.acquire() as conn:
        try:
            await conn.execute('''
                INSERT INTO public.activity_log (chat_id, activity_date, activity_type, mode, reference_id)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (chat_id, activity_date, activity_type) DO NOTHING
            ''', chat_id, today, activity_type, mode, reference_id)
        except Exception as e:
            logger.warning(f"Не удалось записать активность: {e}")
            try:
                await record_internal_metric("activity_log_insert_failed", "record_active_day", 1)
            except Exception as metric_error:
                logger.debug(f"Метрика activity_log_insert_failed тоже не записалась: {metric_error}")

    # 2. Атомарный гейт + обновление счётчиков — одна транзакция dev_pool (WP-7 Ф48).
    #    last_active_date НЕ используется как гейт: её отдельно пишет touch_last_active_date
    #    на любое взаимодействие (нужна DAU/WAU/nudges), и гонка между этими двумя writer'ами
    #    молча теряла инкремент. daily_activity_marker — отдельный, атомарный источник
    #    истины "значимая активность уже засчитана сегодня".
    #    Порядок блокировок — сперва строка user_state (FOR UPDATE), потом маркер —
    #    тот же порядок, что и в _reset_stats() (states/utilities/mydata.py), иначе
    #    конкурентные reset и record_active_day для одного chat_id могут дедлокнуться.
    async with dev_pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow('''
                SELECT
                    active_days_streak,
                    longest_streak,
                    EXISTS (
                        SELECT 1 FROM development.daily_activity_marker
                        WHERE chat_id = $1 AND activity_date = $2::date - 1
                    ) AS had_yesterday
                FROM development.user_state
                WHERE chat_id = $1
                FOR UPDATE
            ''', chat_id, today)

            if current is None:
                logger.warning(f"[record_active_day] нет строки development.user_state для chat_id={chat_id}")
                return

            marker_inserted = await conn.fetchval('''
                INSERT INTO development.daily_activity_marker (chat_id, activity_date)
                VALUES ($1, $2)
                ON CONFLICT (chat_id, activity_date) DO NOTHING
                RETURNING 1
            ''', chat_id, today)

            if not marker_inserted:
                logger.debug(f"📅 {chat_id} уже засчитан сегодня — счётчики не трогаем")
                return

            new_streak, new_longest = compute_streak_update(
                current['active_days_streak'] or 0, current['had_yesterday'], current['longest_streak']
            )

            row = await conn.fetchrow('''
                UPDATE development.user_state
                SET active_days_total = active_days_total + 1,
                    active_days_streak = $2,
                    longest_streak = $3
                WHERE chat_id = $1
                RETURNING active_days_total, active_days_streak, longest_streak
            ''', chat_id, new_streak, new_longest)

        logger.info(f"📅 Активный день для {chat_id}: streak={row['active_days_streak']}, total={row['active_days_total']}")


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
            FROM public.activity_log
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
            INSERT INTO public.service_usage (user_id, service_id, action)
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
            FROM public.service_usage
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
            FROM public.activity_log
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
