"""
Аналитические запросы для /analytics команды.

Агрегирует данные из нескольких таблиц:
- public.users + development.user_state + activity_log → DAU/WAU/MAU, retention
- user_sessions → сессии (длина, частота, entry/exit)
- request_traces → latency, quality
- qa_history → helpful rate
"""

import logging

from db.connection import get_pool, get_learning_pool

logger = logging.getLogger(__name__)


async def get_analytics_report(hours: int = 24) -> dict:
    """Полный аналитический отчёт для /analytics.

    Returns:
        {users, sessions, quality, errors, commands, retention, trends}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await _safe(conn, _get_user_metrics, conn)
        sessions = await _safe(conn, _get_session_metrics, conn, hours)
        quality = await _safe(conn, _get_quality_metrics, conn, hours)
        errors = await _safe(conn, _get_error_metrics, conn, hours)
        commands = await _safe(conn, _get_command_metrics, conn, hours)
        retention = await _safe(conn, _get_retention_metrics, conn)
        trends = await _safe(conn, _get_trend_metrics, conn)

    return {
        'users': users or {'dau': 0, 'wau': 0, 'mau': 0, 'total': 0, 'new_today': 0, 'new_week': 0},
        'sessions': sessions or {'count': 0, 'avg_duration_sec': 0, 'avg_requests': 0, 'entry_points': []},
        'quality': quality or {'total_requests': 0, 'avg_ms': 0, 'p95_ms': 0, 'p50_ms': 0, 'p99_ms': 0, 'red_zone': 0, 'qa_total': 0, 'qa_helpful_rate': 0},
        'errors': errors or {'total': 0, 'error_rate_pct': 0, 'by_category': [], 'by_severity': [], 'l3_plus': 0, 'unknown': 0},
        'commands': commands or {'top': [], 'slowest': []},
        'retention': retention or {'d1': 0, 'd7': 0, 'd30': 0},
        'trends': trends or {'dau_this_week': 0, 'dau_last_week': 0, 'dau_change_pct': 0, 'sessions_this_week': 0, 'sessions_last_week': 0, 'sessions_change_pct': 0},
    }


async def _safe(conn, fn, *args):
    """Обёртка: если подзапрос падает, возвращаем None (не ломаем весь отчёт)."""
    try:
        return await fn(*args)
    except Exception as e:
        logger.warning(f"[Analytics] {fn.__name__} failed: {e}")
        return None


async def _get_user_metrics(conn) -> dict:
    """DAU/WAU/MAU + новые пользователи."""
    row = await conn.fetchrow('''
        SELECT
            COUNT(*) FILTER (WHERE s.last_active_date = (NOW() AT TIME ZONE 'Europe/Moscow')::date) as dau,
            COUNT(*) FILTER (WHERE s.last_active_date >= (NOW() AT TIME ZONE 'Europe/Moscow')::date - 6) as wau,
            COUNT(*) FILTER (WHERE s.last_active_date >= (NOW() AT TIME ZONE 'Europe/Moscow')::date - 29) as mau,
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE u.created_at::date = (NOW() AT TIME ZONE 'Europe/Moscow')::date) as new_today,
            COUNT(*) FILTER (WHERE u.created_at::date >= (NOW() AT TIME ZONE 'Europe/Moscow')::date - 6) as new_week
        FROM development.user_state s
        JOIN public.users u ON u.telegram_id = s.chat_id
        WHERE s.onboarding_completed = TRUE
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
    """Latency + QA quality.

    WP-253 B-port (28 апр): latency мигрирована на learning.public.domain_event
    (event_type='request_traced'). QA quality остаётся на legacy qa_history до G2 решения.
    Параметр `conn` используется только для qa_history — для latency открывается learning pool.
    """
    learning_pool = await get_learning_pool()
    async with learning_pool.acquire() as lc:
        latency = await lc.fetchrow('''
            SELECT
                COUNT(*) AS total_requests,
                COALESCE(AVG((payload->>'total_ms')::numeric), 0)::INTEGER AS avg_ms,
                COALESCE(percentile_cont(0.50) WITHIN GROUP (ORDER BY (payload->>'total_ms')::numeric), 0)::INTEGER AS p50_ms,
                COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->>'total_ms')::numeric), 0)::INTEGER AS p95_ms,
                COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY (payload->>'total_ms')::numeric), 0)::INTEGER AS p99_ms,
                COUNT(*) FILTER (WHERE (payload->>'total_ms')::numeric > 8000) AS red_zone
            FROM domain_event
            WHERE source = 'aist-bot' AND event_type = 'request_traced'
              AND ingested_at > NOW() - ($1 || ' hours')::INTERVAL
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
        'p50_ms': latency['p50_ms'] if latency else 0,
        'p95_ms': latency['p95_ms'] if latency else 0,
        'p99_ms': latency['p99_ms'] if latency else 0,
        'red_zone': latency['red_zone'] if latency else 0,
        'qa_total': qa_total,
        'qa_helpful_rate': helpful_rate,
    }


async def _get_retention_metrics(conn) -> dict:
    """Retention D1/D7/D30 на основе activity_log и users.created_at."""
    result = {}
    for days, label in [(1, 'd1'), (7, 'd7'), (30, 'd30')]:
        row = await conn.fetchrow('''
            WITH cohort AS (
                SELECT s.chat_id, u.created_at::date as cohort_date
                FROM development.user_state s
                JOIN public.users u ON u.telegram_id = s.chat_id
                WHERE s.onboarding_completed = TRUE
                  AND u.created_at < NOW() - make_interval(days := $1)
            ),
            retained AS (
                SELECT DISTINCT c.chat_id
                FROM cohort c
                JOIN activity_log a ON c.chat_id = a.chat_id
                  AND a.activity_date = c.cohort_date + $1
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
            WHERE activity_date >= (NOW() AT TIME ZONE 'Europe/Moscow')::date - 6
        ),
        last_week AS (
            SELECT
                COUNT(DISTINCT chat_id) as dau_avg
            FROM activity_log
            WHERE activity_date BETWEEN (NOW() AT TIME ZONE 'Europe/Moscow')::date - 13 AND (NOW() AT TIME ZONE 'Europe/Moscow')::date - 7
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


async def _get_error_metrics(conn, hours: int) -> dict:
    """Error rate, breakdown by category/severity from error_logs (WP-45)."""
    totals = await conn.fetchrow('''
        SELECT
            SUM(occurrence_count)::BIGINT as total_errors,
            COUNT(*) FILTER (WHERE severity IN ('L3', 'L4')) as l3_plus,
            COUNT(*) FILTER (WHERE category = 'unknown') as unknown
        FROM error_logs
        WHERE last_seen_at > NOW() - ($1 || ' hours')::INTERVAL
    ''', str(hours))

    # Error rate = errors / requests
    # WP-253 B-port (28 апр): request count из learning.domain_event.
    learning_pool = await get_learning_pool()
    async with learning_pool.acquire() as lc:
        requests = await lc.fetchval('''
            SELECT COUNT(*) FROM domain_event
            WHERE source = 'aist-bot' AND event_type = 'request_traced'
              AND ingested_at > NOW() - ($1 || ' hours')::INTERVAL
        ''', str(hours))

    total_err = totals['total_errors'] if totals and totals['total_errors'] else 0
    error_rate = round(total_err / requests * 100, 1) if requests and requests > 0 else 0

    by_category = await conn.fetch('''
        SELECT category, SUM(occurrence_count)::BIGINT as count
        FROM error_logs
        WHERE last_seen_at > NOW() - ($1 || ' hours')::INTERVAL
          AND category IS NOT NULL
        GROUP BY category ORDER BY count DESC LIMIT 5
    ''', str(hours))

    by_severity = await conn.fetch('''
        SELECT severity, COUNT(*) as count
        FROM error_logs
        WHERE last_seen_at > NOW() - ($1 || ' hours')::INTERVAL
          AND severity IS NOT NULL
        GROUP BY severity ORDER BY severity
    ''', str(hours))

    return {
        'total': total_err,
        'error_rate_pct': error_rate,
        'l3_plus': totals['l3_plus'] if totals else 0,
        'unknown': totals['unknown'] if totals else 0,
        'by_category': [dict(r) for r in by_category],
        'by_severity': [dict(r) for r in by_severity],
    }


async def _get_command_metrics(conn, hours: int) -> dict:
    """Per-command request count and latency from learning.domain_event (WP-253 B-port).

    WP-253 B-port (28 апр): миграция с legacy `platform.request_traces` на
    `learning.public.domain_event` (event_type='request_traced').
    Параметр `conn` игнорируется — функция переключилась на learning pool.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as lc:
        top = await lc.fetch('''
            SELECT payload->>'command' AS command, COUNT(*) AS count,
                   COALESCE(AVG((payload->>'total_ms')::numeric), 0)::INTEGER AS avg_ms
            FROM domain_event
            WHERE source = 'aist-bot' AND event_type = 'request_traced'
              AND ingested_at > NOW() - ($1 || ' hours')::INTERVAL
              AND payload->>'command' IS NOT NULL AND payload->>'command' != ''
            GROUP BY 1 ORDER BY count DESC LIMIT 7
        ''', str(hours))

        slowest = await lc.fetch('''
            SELECT payload->>'command' AS command, COUNT(*) AS count,
                   COALESCE(AVG((payload->>'total_ms')::numeric), 0)::INTEGER AS avg_ms,
                   COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->>'total_ms')::numeric), 0)::INTEGER AS p95_ms
            FROM domain_event
            WHERE source = 'aist-bot' AND event_type = 'request_traced'
              AND ingested_at > NOW() - ($1 || ' hours')::INTERVAL
              AND payload->>'command' IS NOT NULL AND payload->>'command' != ''
            GROUP BY 1
            HAVING COUNT(*) >= 3
            ORDER BY avg_ms DESC LIMIT 5
        ''', str(hours))

    return {
        'top': [dict(r) for r in top],
        'slowest': [dict(r) for r in slowest],
    }
