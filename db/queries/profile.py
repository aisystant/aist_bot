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


async def delete_all_user_data(chat_id: int) -> dict:
    """Каскадное удаление ВСЕХ данных пользователя из всех таблиц.

    Порядок: зависимые таблицы → основная (interns).
    Возвращает dict с количеством удалённых строк по таблицам.

    Ref: DP.D.028 (User Data Tiers — протокол удаления).
    """
    pool = await get_pool()
    result = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            # feed_sessions зависит от feed_weeks (FK week_id)
            deleted = await conn.execute(
                '''DELETE FROM feed_sessions
                   WHERE week_id IN (SELECT id FROM feed_weeks WHERE chat_id = $1)''',
                chat_id
            )
            result['feed_sessions'] = _parse_delete_count(deleted)

            # Все остальные таблицы с chat_id / user_id
            tables_chat_id = [
                'answers', 'reminders', 'feed_weeks', 'marathon_content',
                'activity_log', 'qa_history', 'assessments',
                'feedback_reports', 'subscriptions', 'user_sessions',
                'github_connections', 'fsm_states',
            ]
            for table in tables_chat_id:
                deleted = await conn.execute(
                    f'DELETE FROM {table} WHERE chat_id = $1', chat_id
                )
                result[table] = _parse_delete_count(deleted)

            # Таблицы с user_id вместо chat_id
            tables_user_id = ['service_usage', 'request_traces']
            for table in tables_user_id:
                deleted = await conn.execute(
                    f'DELETE FROM {table} WHERE user_id = $1', chat_id
                )
                result[table] = _parse_delete_count(deleted)

            # Основная таблица — последняя
            deleted = await conn.execute(
                'DELETE FROM interns WHERE chat_id = $1', chat_id
            )
            result['interns'] = _parse_delete_count(deleted)

    total = sum(result.values())
    logger.info(f"[DELETE] user {chat_id}: {total} rows deleted from {len(result)} tables")
    return result


async def reset_learning_data(chat_id: int) -> dict:
    """Сброс учебных данных с сохранением профиля.

    Сохраняет: name, occupation, interests, motivation, goals, language,
    schedule_time, subscriptions, github/DT подключения, onboarding_completed.

    Сбрасывает: марафон, лента, ответы, пре-генерированный контент,
    активность, оценки, FSM state.

    Returns: dict с количеством удалённых строк по таблицам.
    """
    pool = await get_pool()
    result = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            # feed_sessions зависит от feed_weeks (FK week_id)
            deleted = await conn.execute(
                '''DELETE FROM feed_sessions
                   WHERE week_id IN (SELECT id FROM feed_weeks WHERE chat_id = $1)''',
                chat_id
            )
            result['feed_sessions'] = _parse_delete_count(deleted)

            # Удаляем учебные данные из связанных таблиц
            learning_tables = [
                'answers', 'feed_weeks', 'marathon_content',
                'activity_log', 'assessments', 'fsm_states',
            ]
            for table in learning_tables:
                deleted = await conn.execute(
                    f'DELETE FROM {table} WHERE chat_id = $1', chat_id
                )
                result[table] = _parse_delete_count(deleted)

            # Сбрасываем поля прогресса в interns (профиль сохраняется)
            await conn.execute('''
                UPDATE interns SET
                    marathon_status = 'not_started',
                    marathon_start_date = NULL,
                    marathon_paused_at = NULL,
                    current_topic_index = 0,
                    completed_topics = '[]',
                    topics_today = 0,
                    last_topic_date = NULL,
                    complexity_level = 1,
                    topics_at_current_complexity = 0,
                    feed_status = 'not_started',
                    feed_started_at = NULL,
                    active_days_total = 0,
                    active_days_streak = 0,
                    longest_streak = 0,
                    last_active_date = NULL,
                    assessment_state = NULL,
                    assessment_date = NULL,
                    stats_reset_date = NULL,
                    current_state = NULL,
                    current_context = '{}',
                    updated_at = NOW()
                WHERE chat_id = $1
            ''', chat_id)
            result['interns_reset'] = 1

    total = sum(result.values())
    logger.info(f"[RESET] user {chat_id}: learning data reset, {total} rows affected across {len(result)} tables")
    return result


def _parse_delete_count(status_str: str) -> int:
    """Извлечь количество из строки 'DELETE N'."""
    try:
        return int(status_str.split()[-1])
    except (ValueError, IndexError):
        return 0
