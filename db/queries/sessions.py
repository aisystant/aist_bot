"""
Управление сессиями пользователей.

Сессия = непрерывный период активности пользователя.
Новая сессия создаётся, если прошло >SESSION_TIMEOUT минут с последнего запроса.

Используется для аналитики: средняя длина сессии, requests/session, entry/exit points.
"""

import json
import logging
from datetime import datetime, timezone

from db.connection import get_pool

logger = logging.getLogger(__name__)

SESSION_TIMEOUT_MINUTES = 30


async def get_or_create_session(chat_id: int, command: str):
    """Найти активную сессию или создать новую.

    Вызывается fire-and-forget из TracingMiddleware на каждый запрос.
    Если active session < SESSION_TIMEOUT — обновить.
    Если нет — финализировать старую + создать новую.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Найти последнюю незакрытую сессию пользователя
        row = await conn.fetchrow('''
            SELECT id, started_at, request_count, commands
            FROM user_sessions
            WHERE chat_id = $1 AND ended_at IS NULL
            ORDER BY started_at DESC
            LIMIT 1
        ''', chat_id)

        now = datetime.now(timezone.utc)

        if row:
            elapsed = (now - row['started_at']).total_seconds() / 60.0
            if elapsed < SESSION_TIMEOUT_MINUTES:
                # Обновить существующую сессию
                commands = json.loads(row['commands']) if row['commands'] else []
                if command not in commands:
                    commands.append(command)
                await conn.execute('''
                    UPDATE user_sessions
                    SET request_count = request_count + 1,
                        exit_point = $2,
                        commands = $3::jsonb
                    WHERE id = $1
                ''', row['id'], command, json.dumps(commands))
                return

            # Сессия истекла — финализировать
            duration = int((now - row['started_at']).total_seconds())
            await conn.execute('''
                UPDATE user_sessions
                SET ended_at = $2,
                    duration_seconds = $3
                WHERE id = $1
            ''', row['id'], now, duration)

        # Создать новую сессию
        await conn.execute('''
            INSERT INTO user_sessions (chat_id, started_at, entry_point, commands)
            VALUES ($1, $2, $3, $4::jsonb)
        ''', chat_id, now, command, json.dumps([command]))


async def finalize_stale_sessions():
    """Финализировать все сессии без ended_at, у которых last update > timeout.

    Вызывается из scheduler midnight cleanup.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('''
            UPDATE user_sessions
            SET ended_at = started_at + (request_count * INTERVAL '1 second' * 30),
                duration_seconds = EXTRACT(EPOCH FROM
                    (started_at + (request_count * INTERVAL '1 second' * 30)) - started_at
                )::INTEGER
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
    pool = await get_pool()
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
