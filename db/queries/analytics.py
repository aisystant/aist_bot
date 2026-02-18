"""
Аналитические запросы для /analytics команды.

Агрегирует данные из нескольких таблиц:
- interns + activity_log → DAU/WAU/MAU, retention
- user_sessions → сессии (длина, частота, entry/exit)
- request_traces → latency, quality
- qa_history → helpful rate
"""

import logging

from db.connection import get_pool

logger = logging.getLogger(__name__)


async def get_analytics_report(hours: int = 24) -> dict:
    """Полный аналитический отчёт для /analytics.

    Returns:
        {users, sessions, quality, retention, trends}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await _get_user_metrics(conn)
        sessions = await _get_session_metrics(conn, hours)
        quality = await _get_quality_metrics(conn, hours)
        retention = await _get_retention_metrics(conn)
        trends = await _get_trend_metrics(conn)

    return {
        'users': users,
        'sessions': sessions,
        'quality': quality,
        'retention': retention,
        'trends': trends,
    }


async def _get_user_metrics(conn) -> dict:
    """DAU/WAU/MAU + новые пользователи."""
    row = await conn.fetchrow('''
        SELECT
            COUNT(*) FILTER (WHERE last_active_date = CURRENT_DATE) as dau,
            COUNT(*) FILTER (WHERE last_active_date >= CURRENT_DATE - 6) as wau,
            COUNT(*) FILTER (WHERE last_active_date >= CURRENT_DATE - 29) as mau,
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE created_at::date = CURRENT_DATE) as new_today,
            COUNT(*) FILTER (WHERE created_at::date >= CURRENT_DATE - 6) as new_week
        FROM interns
        WHERE onboarding_completed = TRUE
    ''')
    return dict(row) if row else {
        'dau': 0, 'wau': 0, 'mau': 0, 'total': 0,
        'new_today': 0, 'new_week': 0,
    }


async def _get_session_metrics(conn, hours: int) -> dict:
    """Статистика сессий."""
    row = await conn.fetchrow('''
        SELECT
            COUNT(*) as count,
            COALESCE(AVG(duration_seconds), 0)::INTEGER as avg_duration_sec,
            COALESCE(AVG(request_count), 0)::REAL as avg_requests
        FROM user_sessions
        WHERE started_at > NOW() - ($1 || ' hours')::INTERVAL
          AND duration_seconds IS NOT NULL
    ''', str(hours))

    entry_points = await conn.fetch('''
        SELECT entry_point as point, COUNT(*) as count
        FROM user_sessions
        WHERE started_at > NOW() - ($1 || ' hours')::INTERVAL
          AND entry_point IS NOT NULL
        GROUP BY entry_point
        ORDER BY count DESC
        LIMIT 5
    ''', str(hours))

    return {
        'count': row['count'] if row else 0,
        'avg_duration_sec': row['avg_duration_sec'] if row else 0,
        'avg_requests': round(row['avg_requests'], 1) if row else 0,
        'entry_points': [dict(r) for r in entry_points],
    }


async def _get_quality_metrics(conn, hours: int) -> dict:
    """Latency + QA quality."""
    latency = await conn.fetchrow('''
        SELECT
            COUNT(*) as total_requests,
            COALESCE(AVG(total_ms), 0)::INTEGER as avg_ms,
            COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms), 0)::INTEGER as p95_ms,
            COUNT(*) FILTER (WHERE total_ms > 8000) as red_zone
        FROM request_traces
        WHERE created_at > NOW() - ($1 || ' hours')::INTERVAL
    ''', str(hours))

    qa = await conn.fetchrow('''
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE helpful = TRUE) as helpful,
            COUNT(*) FILTER (WHERE helpful = FALSE) as not_helpful
        FROM qa_history
        WHERE created_at > NOW() - ($1 || ' hours')::INTERVAL
    ''', str(hours))

    qa_total = qa['total'] if qa else 0
    qa_helpful = qa['helpful'] if qa else 0
    helpful_rate = round(qa_helpful / qa_total * 100) if qa_total > 0 else 0

    return {
        'total_requests': latency['total_requests'] if latency else 0,
        'avg_ms': latency['avg_ms'] if latency else 0,
        'p95_ms': latency['p95_ms'] if latency else 0,
        'red_zone': latency['red_zone'] if latency else 0,
        'qa_total': qa_total,
        'qa_helpful_rate': helpful_rate,
    }


async def _get_retention_metrics(conn) -> dict:
    """Retention D1/D7/D30 на основе activity_log и interns.created_at."""
    result = {}
    for days, label in [(1, 'd1'), (7, 'd7'), (30, 'd30')]:
        row = await conn.fetchrow('''
            WITH cohort AS (
                SELECT chat_id, created_at::date as cohort_date
                FROM interns
                WHERE onboarding_completed = TRUE
                  AND created_at < NOW() - ($1 || ' days')::INTERVAL
            ),
            retained AS (
                SELECT DISTINCT c.chat_id
                FROM cohort c
                JOIN activity_log a ON c.chat_id = a.chat_id
                  AND a.activity_date = c.cohort_date + $1::INTEGER
            )
            SELECT
                (SELECT COUNT(*) FROM cohort) as cohort_size,
                (SELECT COUNT(*) FROM retained) as retained_count
        ''', days)

        cohort_size = row['cohort_size'] if row else 0
        retained = row['retained_count'] if row else 0
        result[label] = round(retained / cohort_size * 100) if cohort_size > 0 else 0

    return result


async def _get_trend_metrics(conn) -> dict:
    """Week-over-week trends: DAU, sessions."""
    row = await conn.fetchrow('''
        WITH this_week AS (
            SELECT
                COUNT(DISTINCT chat_id) as dau_avg
            FROM activity_log
            WHERE activity_date >= CURRENT_DATE - 6
        ),
        last_week AS (
            SELECT
                COUNT(DISTINCT chat_id) as dau_avg
            FROM activity_log
            WHERE activity_date BETWEEN CURRENT_DATE - 13 AND CURRENT_DATE - 7
        )
        SELECT
            tw.dau_avg as this_week_dau,
            lw.dau_avg as last_week_dau
        FROM this_week tw, last_week lw
    ''')

    this_w = row['this_week_dau'] if row else 0
    last_w = row['last_week_dau'] if row else 0
    dau_change = round((this_w - last_w) / last_w * 100) if last_w > 0 else 0

    # Session trends
    sess_row = await conn.fetchrow('''
        WITH this_week AS (
            SELECT COUNT(*) as cnt FROM user_sessions
            WHERE started_at >= NOW() - INTERVAL '7 days'
        ),
        last_week AS (
            SELECT COUNT(*) as cnt FROM user_sessions
            WHERE started_at BETWEEN NOW() - INTERVAL '14 days' AND NOW() - INTERVAL '7 days'
        )
        SELECT tw.cnt as this_week, lw.cnt as last_week
        FROM this_week tw, last_week lw
    ''')

    this_s = sess_row['this_week'] if sess_row else 0
    last_s = sess_row['last_week'] if sess_row else 0
    sess_change = round((this_s - last_s) / last_s * 100) if last_s > 0 else 0

    return {
        'dau_this_week': this_w,
        'dau_last_week': last_w,
        'dau_change_pct': dau_change,
        'sessions_this_week': this_s,
        'sessions_last_week': last_s,
        'sessions_change_pct': sess_change,
    }
