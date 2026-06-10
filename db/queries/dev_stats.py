from __future__ import annotations

"""
Запросы аналитики для разработчика (/stats, /usage, /qa, /health).
"""

from typing import List

from config import get_logger
from db.connection import get_pool, get_journal_pool, get_learning_pool, get_health_pool

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
                COUNT(*) FILTER (WHERE last_active_date = (NOW() AT TIME ZONE 'Europe/Moscow')::date) AS active_today,
                COUNT(*) FILTER (WHERE last_active_date >= (NOW() AT TIME ZONE 'Europe/Moscow')::date - INTERVAL '7 days') AS active_week,
                COUNT(*) FILTER (WHERE marathon_status = 'active') AS marathon_active,
                COUNT(*) FILTER (WHERE marathon_status = 'completed') AS marathon_completed,
                COUNT(*) FILTER (WHERE marathon_status = 'paused') AS marathon_paused,
                COUNT(*) FILTER (WHERE feed_status = 'active') AS feed_active,
                COUNT(*) FILTER (WHERE marathon_status = 'active' AND feed_status = 'active') AS both_active,
                ROUND(AVG(active_days_total), 1) AS avg_active_days,
                ROUND(AVG(active_days_streak) FILTER (WHERE active_days_streak > 0), 1) AS avg_streak,
                MAX(longest_streak) AS max_streak,
                ROUND(AVG(complexity_level), 1) AS avg_complexity
            FROM development.user_state
            WHERE onboarding_completed = TRUE
        ''')
        return dict(row) if row else {}


async def get_language_distribution() -> List[dict]:
    """Распределение по языкам."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT COALESCE(u.language, 'ru') AS lang, COUNT(*) AS cnt
            FROM development.user_state s
            JOIN public.users u ON u.telegram_id = s.chat_id
            WHERE s.onboarding_completed = TRUE
            GROUP BY u.language ORDER BY cnt DESC
        ''')
        return [dict(r) for r in rows]


async def get_complexity_distribution() -> List[dict]:
    """Распределение по уровням сложности."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT complexity_level AS lvl, COUNT(*) AS cnt
            FROM development.user_state WHERE onboarding_completed = TRUE
            GROUP BY complexity_level ORDER BY complexity_level
        ''')
        return [dict(r) for r in rows]


async def get_integration_stats() -> dict:
    """Статистика интеграций (GitHub, Assessment, ЦД)."""
    # github_connections → bot_data
    pool = await get_pool()
    async with pool.acquire() as conn:
        github_row = await conn.fetchrow(
            'SELECT COUNT(*) AS github_connected FROM github_connections'
        )
    # assessments → learning BD (мигрировано Phase 5 G5)
    learning_pool = await get_learning_pool()
    async with learning_pool.acquire() as lc:
        assess_row = await lc.fetchrow('''
            SELECT
                COUNT(DISTINCT chat_id) AS assessed_users,
                COUNT(*) AS total_assessments
            FROM assessments
        ''')
    return {
        'github_connected': github_row['github_connected'] if github_row else 0,
        'assessed_users': assess_row['assessed_users'] if assess_row else 0,
        'total_assessments': assess_row['total_assessments'] if assess_row else 0,
    }


# === /usage — популярность сервисов ===

async def get_global_service_usage(limit: int = 15) -> List[dict]:
    """Топ сервисов по использованию (все пользователи)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch('''
                SELECT service_id, COUNT(*) AS cnt,
                       COUNT(DISTINCT user_id) AS users
                FROM service_usage
                GROUP BY service_id
                ORDER BY cnt DESC
                LIMIT $1
            ''', limit)
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"service_usage query failed: {e}")
            return []


async def get_schedule_distribution() -> List[dict]:
    """Распределение по времени расписания (часы)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT
                COALESCE(schedule_time, '09:00')::TEXT AS hour,
                COUNT(*) AS cnt
            FROM development.user_state
            WHERE onboarding_completed = TRUE
              AND (marathon_status = 'active' OR feed_status = 'active')
            GROUP BY schedule_time
            ORDER BY hour
        ''')
        return [dict(r) for r in rows]


# === /qa — качество консультаций ===

async def get_qa_stats() -> dict:
    """Статистика консультаций (WP-268 Phase 3 Block 2: журнал БД)."""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE helpful = TRUE) AS helpful,
                COUNT(*) FILTER (WHERE helpful = FALSE) AS not_helpful,
                COUNT(*) FILTER (WHERE helpful IS NULL) AS no_feedback,
                COUNT(*) FILTER (WHERE user_comment IS NOT NULL AND user_comment != '') AS with_comments,
                COUNT(DISTINCT chat_id) AS unique_users,
                COUNT(*) FILTER (WHERE created_at >= (NOW() AT TIME ZONE 'Europe/Moscow')::date) AS today,
                COUNT(*) FILTER (WHERE created_at >= (NOW() AT TIME ZONE 'Europe/Moscow')::date - INTERVAL '7 days') AS this_week
            FROM qa_history
        ''')
        return dict(row) if row else {}


async def get_qa_top_topics(limit: int = 10) -> List[dict]:
    """Топ тем вопросов (WP-268 Phase 3 Block 2: журнал БД)."""
    pool = await get_journal_pool()
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
    results = []

    # bot_data tables
    # WP-268 Phase 3 Block 2: qa_history теперь в journal БД
    # WP-268 Phase 5 G5: answers/activity_log/assessments → learning BD (см. ниже)
    pool = await get_pool()
    async with pool.acquire() as conn:
        for table in [
            'public.users', 'development.user_state',
            'feed_weeks', 'feed_sessions',
            'feedback_reports', 'github_connections', 'service_usage',
            'marathon_content',
        ]:
            try:
                row = await conn.fetchrow(f'SELECT COUNT(*) AS cnt FROM {table}')
                results.append({'table': table, 'count': row['cnt']})
            except Exception:
                results.append({'table': table, 'count': -1})

    # learning BD tables (WP-268 Phase 5 G5)
    learning_pool = await get_learning_pool()
    async with learning_pool.acquire() as lc:
        for table in ['answers', 'activity_log', 'assessments']:
            try:
                row = await lc.fetchrow(f'SELECT COUNT(*) AS cnt FROM {table}')
                results.append({'table': f'learning.{table}', 'count': row['cnt']})
            except Exception:
                results.append({'table': f'learning.{table}', 'count': -1})

    # health BD tables (WP-268 Phase 5 G5 Tier2)
    health_pool = await get_health_pool()
    async with health_pool.acquire() as hc:
        for table in ['error_logs', 'user_sessions', 'pending_fixes']:
            try:
                row = await hc.fetchrow(f'SELECT COUNT(*) AS cnt FROM {table}')
                results.append({'table': f'health.{table}', 'count': row['cnt']})
            except Exception:
                results.append({'table': f'health.{table}', 'count': -1})

    return results


async def get_pending_content_count() -> int:
    """Количество ожидающего контента марафона."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM marathon_content WHERE status = 'pending'"
        )
        return row['cnt'] if row else 0


# === /delivery — отчёт о доставке марафона ===

async def get_delivery_report() -> dict:
    """Отчёт: кто из активных участников марафона получил урок сегодня.

    Поддерживает два движка:
    - Старый: marathon_content в bot_data (notification_sent_at)
    - Новый: learning.marathon_queue (status='sent') + learning.marathon_state (checkin)
    """
    from datetime import datetime, timezone, timedelta
    MOSCOW_TZ = timezone(timedelta(hours=3))

    pool = await get_pool()
    learning_pool = await get_learning_pool()

    async with pool.acquire() as conn:
        now_msk = datetime.now(MOSCOW_TZ)

        # Active marathon users
        active = await conn.fetch('''
            SELECT s.chat_id, u.tg_username, s.schedule_time
            FROM development.user_state s
            JOIN public.users u ON u.telegram_id = s.chat_id
            WHERE s.marathon_status = 'active'
              AND s.onboarding_completed = TRUE
            ORDER BY s.schedule_time
        ''')

        # Старый движок: marathon_content в bot_data
        content_today = await conn.fetch('''
            SELECT DISTINCT ON (mc.chat_id)
                mc.chat_id, mc.status, mc.created_at,
                mc.notification_sent_at
            FROM marathon_content mc
            WHERE mc.notification_sent_at >= (NOW() AT TIME ZONE 'Europe/Moscow')::date
               OR (mc.notification_sent_at IS NULL
                   AND mc.created_at >= (NOW() AT TIME ZONE 'Europe/Moscow')::date)
            ORDER BY mc.chat_id, COALESCE(mc.notification_sent_at, mc.created_at) DESC
        ''')
        content_map = {r['chat_id']: r for r in content_today}

    # Новый движок: learning.marathon_queue (sent today) + marathon_state (checkin today)
    async with learning_pool.acquire() as lconn:
        queue_today = await lconn.fetch('''
            SELECT DISTINCT ON (user_id)
                user_id, status, sent_at
            FROM learning.marathon_queue
            WHERE sent_at >= (NOW() AT TIME ZONE 'Europe/Moscow')::date
              AND status = 'sent'
            ORDER BY user_id, sent_at DESC
        ''')
        queue_map = {r['user_id']: r for r in queue_today}

        # Checkin сегодня = пользователь открыл урок (новый движок)
        checkins_today = await lconn.fetch('''
            SELECT DISTINCT user_id
            FROM learning.marathon_state
            WHERE check_in_at >= (NOW() AT TIME ZONE 'Europe/Moscow')::date
        ''')
        checkin_set = {r['user_id'] for r in checkins_today}

    users = []
    counts = {'sent_read': 0, 'sent_unread': 0, 'not_yet': 0, 'missed': 0}

    for u in active:
        chat_id = u['chat_id']
        sched = u['schedule_time'] or '09:00'
        entry = {
            'chat_id': chat_id,
            'username': u['tg_username'],
            'schedule': sched,
        }

        c = content_map.get(chat_id)
        if c:
            # Старый движок: запись есть в marathon_content (bot_data)
            ts = c['notification_sent_at'] or c['created_at']
            ts_msk = ts + timedelta(hours=3) if ts.tzinfo is None else ts.astimezone(MOSCOW_TZ)
            entry['time'] = ts_msk.strftime('%H:%M')
            if c['status'] == 'delivered':
                entry['status'] = 'sent_read'
                counts['sent_read'] += 1
            else:
                entry['status'] = 'sent_unread'
                counts['sent_unread'] += 1
        elif chat_id in queue_map:
            # Новый движок: урок отправлен через learning.marathon_queue
            q = queue_map[chat_id]
            ts = q['sent_at']
            ts_msk = ts + timedelta(hours=3) if ts.tzinfo is None else ts.astimezone(MOSCOW_TZ)
            entry['time'] = ts_msk.strftime('%H:%M')
            entry['engine'] = 'new'
            if chat_id in checkin_set:
                entry['status'] = 'sent_read'
                counts['sent_read'] += 1
            else:
                entry['status'] = 'sent_unread'
                counts['sent_unread'] += 1
        else:
            # Контента нет — либо ещё не время, либо пропущено
            try:
                h, m = map(int, sched.split(':'))
                sched_dt = now_msk.replace(hour=h, minute=m, second=0)
                if now_msk < sched_dt:
                    entry['status'] = 'not_yet'
                    counts['not_yet'] += 1
                else:
                    entry['status'] = 'missed'
                    counts['missed'] += 1
            except ValueError:
                entry['status'] = 'missed'
                counts['missed'] += 1

        users.append(entry)

    return {
        'users': users,
        'report_date': now_msk.strftime('%d.%m.%Y'),
        'summary': {
            'active': len(active),
            'sent': counts['sent_read'] + counts['sent_unread'],
            **counts,
        },
    }
