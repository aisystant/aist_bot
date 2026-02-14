"""
Запросы аналитики для разработчика (/stats, /usage, /qa, /health).
"""

from typing import List

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


# === /stats — пользователи и активность ===

async def get_user_stats() -> dict:
    """Общая статистика по пользователям."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE onboarding_completed = TRUE) AS onboarded,
                COUNT(*) FILTER (WHERE last_active_date = CURRENT_DATE) AS active_today,
                COUNT(*) FILTER (WHERE last_active_date >= CURRENT_DATE - INTERVAL '7 days') AS active_week,
                COUNT(*) FILTER (WHERE marathon_status = 'active') AS marathon_active,
                COUNT(*) FILTER (WHERE marathon_status = 'completed') AS marathon_completed,
                COUNT(*) FILTER (WHERE marathon_status = 'paused') AS marathon_paused,
                COUNT(*) FILTER (WHERE feed_status = 'active') AS feed_active,
                COUNT(*) FILTER (WHERE marathon_status = 'active' AND feed_status = 'active') AS both_active,
                ROUND(AVG(active_days_total), 1) AS avg_active_days,
                ROUND(AVG(active_days_streak) FILTER (WHERE active_days_streak > 0), 1) AS avg_streak,
                MAX(longest_streak) AS max_streak,
                ROUND(AVG(complexity_level), 1) AS avg_complexity
            FROM interns
            WHERE onboarding_completed = TRUE
        ''')
        return dict(row) if row else {}


async def get_language_distribution() -> List[dict]:
    """Распределение по языкам."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT COALESCE(language, 'ru') AS lang, COUNT(*) AS cnt
            FROM interns WHERE onboarding_completed = TRUE
            GROUP BY language ORDER BY cnt DESC
        ''')
        return [dict(r) for r in rows]


async def get_complexity_distribution() -> List[dict]:
    """Распределение по уровням сложности."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT complexity_level AS lvl, COUNT(*) AS cnt
            FROM interns WHERE onboarding_completed = TRUE
            GROUP BY complexity_level ORDER BY complexity_level
        ''')
        return [dict(r) for r in rows]


async def get_integration_stats() -> dict:
    """Статистика интеграций (GitHub, Assessment, ЦД)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                (SELECT COUNT(*) FROM github_connections) AS github_connected,
                (SELECT COUNT(DISTINCT chat_id) FROM assessments) AS assessed_users,
                (SELECT COUNT(*) FROM assessments) AS total_assessments
        ''')
        return dict(row) if row else {}


# === /usage — популярность сервисов ===

async def get_global_service_usage(limit: int = 15) -> List[dict]:
    """Топ сервисов по использованию (все пользователи)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT service_id, COUNT(*) AS cnt,
                   COUNT(DISTINCT user_id) AS users
            FROM service_usage
            GROUP BY service_id
            ORDER BY cnt DESC
            LIMIT $1
        ''', limit)
        return [dict(r) for r in rows]


async def get_schedule_distribution() -> List[dict]:
    """Распределение по времени расписания (часы)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT
                COALESCE(schedule_time, '09:00')::TEXT AS hour,
                COUNT(*) AS cnt
            FROM interns
            WHERE onboarding_completed = TRUE
              AND (marathon_status = 'active' OR feed_status = 'active')
            GROUP BY schedule_time
            ORDER BY hour
        ''')
        return [dict(r) for r in rows]


# === /qa — качество консультаций ===

async def get_qa_stats() -> dict:
    """Статистика консультаций."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE helpful = TRUE) AS helpful,
                COUNT(*) FILTER (WHERE helpful = FALSE) AS not_helpful,
                COUNT(*) FILTER (WHERE helpful IS NULL) AS no_feedback,
                COUNT(*) FILTER (WHERE user_comment IS NOT NULL AND user_comment != '') AS with_comments,
                COUNT(DISTINCT chat_id) AS unique_users,
                COUNT(*) FILTER (WHERE created_at >= CURRENT_DATE) AS today,
                COUNT(*) FILTER (WHERE created_at >= CURRENT_DATE - INTERVAL '7 days') AS this_week
            FROM qa_history
        ''')
        return dict(row) if row else {}


async def get_qa_top_topics(limit: int = 10) -> List[dict]:
    """Топ тем вопросов."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT context_topic AS topic, COUNT(*) AS cnt
            FROM qa_history
            WHERE context_topic IS NOT NULL AND context_topic != ''
            GROUP BY context_topic
            ORDER BY cnt DESC
            LIMIT $1
        ''', limit)
        return [dict(r) for r in rows]


# === /health — техническое состояние ===

async def get_table_sizes() -> List[dict]:
    """Количество записей в каждой таблице."""
    pool = await get_pool()
    tables = [
        'interns', 'answers', 'activity_log', 'qa_history',
        'feed_weeks', 'feed_sessions', 'assessments',
        'feedback_reports', 'github_connections', 'service_usage',
        'marathon_content',
    ]
    results = []
    async with pool.acquire() as conn:
        for table in tables:
            try:
                row = await conn.fetchrow(f'SELECT COUNT(*) AS cnt FROM {table}')
                results.append({'table': table, 'count': row['cnt']})
            except Exception:
                results.append({'table': table, 'count': -1})
    return results


async def get_pending_content_count() -> int:
    """Количество ожидающего контента марафона."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM marathon_content WHERE status = 'pending'"
        )
        return row['cnt'] if row else 0
