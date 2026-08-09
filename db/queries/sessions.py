from __future__ import annotations

"""
Управление сессиями пользователей.

Сессия = непрерывный период активности пользователя.
Новая сессия создаётся, если прошло >SESSION_TIMEOUT минут с последнего запроса.

Используется для аналитики: средняя длина сессии, requests/session, entry/exit points.
"""

import json
import logging
from datetime import datetime, timezone

from db.connection import get_health_pool
from db.queries.events import log_event

logger = logging.getLogger(__name__)

SESSION_TIMEOUT_MINUTES = 30


def _last_activity_at(row):
    """Вернуть фактическое время последнего запроса с legacy fallback."""
    return row['ended_at'] or row['started_at']


def _duration_seconds(started_at, last_activity_at) -> int:
    """Длительность активного интервала от первого до последнего запроса."""
    return max(0, int((last_activity_at - started_at).total_seconds()))


async def get_or_create_session(chat_id: int, command: str):
    """Найти активную сессию или создать новую.

    Вызывается fire-and-forget из TracingMiddleware на каждый запрос.
    Сессия продолжается, если с последнего запроса прошло меньше timeout.
    ended_at всегда означает последний запрос, поэтому duration не включает
    часы отсутствия пользователя и не требует поздней финализации.
    """
    pool = await get_health_pool()
    async with pool.acquire() as conn:
        # Последняя сессия может быть продолжена, если пользователь вернулся
        # раньше timeout. Новые строки всегда имеют ended_at; NULL остаётся
        # только у legacy-записей старой модели.
        row = await conn.fetchrow('''
            SELECT id, started_at, ended_at, request_count, commands
            FROM user_sessions
            WHERE chat_id = $1
            ORDER BY started_at DESC
            LIMIT 1
        ''', chat_id)

        now = datetime.now(timezone.utc)

        if row:
            idle_minutes = (now - _last_activity_at(row)).total_seconds() / 60.0
            if idle_minutes < SESSION_TIMEOUT_MINUTES:
                commands = json.loads(row['commands']) if row['commands'] else []
                if command not in commands:
                    commands.append(command)
                await conn.execute('''
                    UPDATE user_sessions
                    SET ended_at = $2,
                        duration_seconds = $3,
                        request_count = request_count + 1,
                        exit_point = $4,
                        commands = $5::jsonb
                    WHERE id = $1
                ''',
                    row['id'],
                    now,
                    _duration_seconds(row['started_at'], now),
                    command,
                    json.dumps(commands),
                )
                return

            if row['ended_at'] is None:
                # Legacy-строка не знает последнего запроса. Не выдумываем
                # активность: фиксируем нулевую известную длительность.
                await conn.execute('''
                    UPDATE user_sessions
                    SET ended_at = started_at,
                        duration_seconds = 0
                    WHERE id = $1
                ''', row['id'])

        # Создать новую сессию
        await conn.execute('''
            INSERT INTO user_sessions
                (chat_id, started_at, ended_at, duration_seconds,
                 request_count, entry_point, exit_point, commands)
            VALUES ($1, $2, $2, 0, 1, $3, $3, $4::jsonb)
        ''', chat_id, now, command, json.dumps([command]))

        # WP-151 Ф3: session_start с returning_after_days
        returning_after_days = None
        if row:
            returning_after_days = max(0, (now - _last_activity_at(row)).days)
        await log_event(chat_id, 'session_start', {
            'entry_point': command,
            'returning_after_days': returning_after_days,
        })


async def finalize_stale_sessions():
    """Закрыть оставшиеся legacy-сессии без ended_at.

    Новая модель обновляет ended_at на каждом запросе и сюда не попадает.
    Для legacy-строк неизвестно время последнего запроса, поэтому не создаём
    искусственную длительность из request_count.
    """
    pool = await get_health_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('''
            UPDATE user_sessions
            SET ended_at = started_at,
                duration_seconds = 0
            WHERE ended_at IS NULL
              AND started_at < NOW() - INTERVAL '30 minutes'
        ''')
        count = int(result.split()[-1]) if result and result != 'UPDATE 0' else 0
        if count > 0:
            logger.info(f"[Sessions] Finalized {count} stale sessions")
        return count


async def get_session_stats(hours: int = 24) -> dict:
    """Статистика сессий для /analytics.

    Returns:
        {count, avg_duration_sec, avg_requests, entry_points: [{point, count}]}
    """
    pool = await get_health_pool()
    async with pool.acquire() as conn:
        stats = await conn.fetchrow('''
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
            'count': stats['count'] if stats else 0,
            'avg_duration_sec': stats['avg_duration_sec'] if stats else 0,
            'avg_requests': round(stats['avg_requests'], 1) if stats else 0,
            'entry_points': [dict(r) for r in entry_points],
        }
