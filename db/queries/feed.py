"""
Запросы для режима Лента.
"""

import json
from datetime import date, timedelta
from typing import List, Optional

from config import get_logger, FeedWeekStatus
from db.connection import get_pool

logger = get_logger(__name__)


async def create_feed_week(chat_id: int, suggested_topics: List[str] = None,
                          accepted_topics: List[str] = None,
                          status: str = FeedWeekStatus.PLANNING) -> dict:
    """Создать новую неделю Ленты"""
    from .users import moscow_today

    pool = await get_pool()
    today = moscow_today()
    week_start = today - timedelta(days=today.weekday())  # Понедельник

    # Получить номер недели
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT COALESCE(MAX(week_number), 0) + 1 as next_week
            FROM feed_weeks WHERE chat_id = $1
        ''', chat_id)
        week_number = row['next_week']

        result = await conn.fetchrow('''
            INSERT INTO feed_weeks
            (chat_id, week_number, week_start, suggested_topics, accepted_topics, status)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, week_number
        ''', chat_id, week_number, week_start,
            json.dumps(suggested_topics or []),
            json.dumps(accepted_topics or []),
            status)

        return {'id': result['id'], 'week_number': result['week_number']}


async def get_current_feed_week(chat_id: int) -> Optional[dict]:
    """Получить текущую (активную или в планировании) неделю"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT * FROM feed_weeks
            WHERE chat_id = $1 AND status IN ($2, $3)
            ORDER BY
                CASE WHEN status = $3 THEN 0 ELSE 1 END,
                created_at DESC
            LIMIT 1
        ''', chat_id, FeedWeekStatus.PLANNING, FeedWeekStatus.ACTIVE)

        if row:
            return {
                'id': row['id'],
                'chat_id': row['chat_id'],
                'week_number': row['week_number'],
                'week_start': row['week_start'],
                'suggested_topics': json.loads(row['suggested_topics']),
                'accepted_topics': json.loads(row['accepted_topics']),
                'current_day': row['current_day'],
                'status': row['status'],
                'created_at': row['created_at']
            }
        return None


async def update_feed_week(week_id: int, updates: dict):
    """Обновить неделю Ленты"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        for key, value in updates.items():
            if key in ['suggested_topics', 'accepted_topics']:
                value = json.dumps(value) if not isinstance(value, str) else value

            await conn.execute(
                f'UPDATE feed_weeks SET {key} = $1 WHERE id = $2',
                value, week_id
            )


async def create_feed_session(week_id: int, day_number: int,
                             topic_title: str, content: dict,
                             session_date: date,
                             status: str = 'active') -> dict | None:
    """Создать или обновить сессию Ленты (UPSERT).

    UPSERT по (week_id, session_date):
    - Если сессии нет → INSERT
    - Если есть pending → UPDATE (перегенерация)
    - Если есть active/completed → пропуск (не затирает пользовательские данные)

    Returns:
        dict сессии или None (если active/completed уже существует)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchrow('''
            INSERT INTO feed_sessions
            (week_id, day_number, topic_title, content, session_date, status)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (week_id, session_date) DO UPDATE SET
                content = EXCLUDED.content,
                day_number = EXCLUDED.day_number,
                topic_title = EXCLUDED.topic_title,
                status = EXCLUDED.status,
                created_at = NOW()
            WHERE feed_sessions.status = 'pending'
            RETURNING id, status
        ''', week_id, day_number, topic_title, json.dumps(content), session_date, status)

        if not result:
            return None

        return {
            'id': result['id'],
            'week_id': week_id,
            'day_number': day_number,
            'topic_title': topic_title,
            'content': content,
            'session_date': session_date,
            'status': result['status']
        }


async def update_feed_session(session_id: int, updates: dict):
    """Обновить сессию Ленты"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        for key, value in updates.items():
            if key == 'content':
                value = json.dumps(value) if not isinstance(value, str) else value

            await conn.execute(
                f'UPDATE feed_sessions SET {key} = $1 WHERE id = $2',
                value, session_id
            )


async def get_feed_session_by_id(session_id: int) -> Optional[dict]:
    """Получить сессию по ID"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM feed_sessions WHERE id = $1',
            session_id
        )

        if row:
            return {
                'id': row['id'],
                'week_id': row['week_id'],
                'day_number': row['day_number'],
                'topic_title': row['topic_title'],
                'content': json.loads(row['content']) if row['content'] else {},
                'fixation_text': row.get('fixation_text', ''),
                'session_date': row['session_date'],
                'status': row['status'],
                'completed_at': row.get('completed_at'),
            }
        return None


async def get_feed_session(week_id: int, session_date: date) -> Optional[dict]:
    """Получить сессию по week_id и дате.

    При дубликатах (несколько сессий на один день) приоритет:
    completed > active, затем по created_at DESC.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT * FROM feed_sessions
               WHERE week_id = $1 AND session_date = $2
               ORDER BY
                   CASE WHEN status = 'completed' THEN 0
                        WHEN status = 'active' THEN 1
                        WHEN status = 'pending' THEN 2
                        ELSE 3 END,
                   created_at DESC
               LIMIT 1''',
            week_id, session_date
        )

        if row:
            return {
                'id': row['id'],
                'week_id': row['week_id'],
                'day_number': row['day_number'],
                'topic_title': row['topic_title'],
                'content': json.loads(row['content']) if row['content'] else {},
                'fixation_text': row.get('fixation_text', ''),
                'session_date': row['session_date'],
                'status': row['status'],
                'completed_at': row.get('completed_at'),
            }
        return None


async def get_incomplete_feed_session(week_id: int, today_only: bool = True) -> Optional[dict]:
    """Получить незавершённую сессию (status = 'active') для недели.

    Args:
        today_only: если True (default), ищет только сегодняшние active сессии.
            Вчерашние active авто-экспайрятся scheduler'ом / cleanup'ом.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if today_only:
            today = moscow_today()
            row = await conn.fetchrow(
                '''SELECT * FROM feed_sessions
                   WHERE week_id = $1 AND status = 'active' AND session_date = $2
                   LIMIT 1''',
                week_id, today
            )
        else:
            row = await conn.fetchrow(
                '''SELECT * FROM feed_sessions
                   WHERE week_id = $1 AND status = 'active'
                   ORDER BY session_date DESC
                   LIMIT 1''',
                week_id
            )

        if row:
            return {
                'id': row['id'],
                'week_id': row['week_id'],
                'day_number': row['day_number'],
                'topic_title': row['topic_title'],
                'content': json.loads(row['content']) if row['content'] else {},
                'fixation_text': row.get('fixation_text', ''),
                'session_date': row['session_date'],
                'status': row['status'],
                'completed_at': row.get('completed_at'),
            }
        return None


async def expire_old_feed_sessions(chat_id: int):
    """Авто-экспайр незакрытых сессий (pending/active) за прошлые дни.

    Вызывается scheduler'ом перед созданием новой сессии.
    Гарантирует замкнутый lifecycle: каждая сессия → терминальный статус.
    """
    today = moscow_today()
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            '''UPDATE feed_sessions SET status = 'expired'
               WHERE week_id IN (SELECT id FROM feed_weeks WHERE chat_id = $1)
                 AND status IN ('pending', 'active')
                 AND session_date < $2''',
            chat_id, today,
        )
        if result and result != 'UPDATE 0':
            logger.info(f"[Feed] Auto-expired old sessions for {chat_id}: {result}")


async def delete_feed_sessions(week_id: int, keep_completed: bool = False):
    """Удалить сессии недели (при смене тем).

    Args:
        keep_completed: если True, сохраняет терминальные сессии (completed/skipped/expired).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if keep_completed:
            deleted = await conn.execute(
                "DELETE FROM feed_sessions WHERE week_id = $1 AND status NOT IN ('completed', 'skipped', 'expired')",
                week_id
            )
        else:
            deleted = await conn.execute(
                'DELETE FROM feed_sessions WHERE week_id = $1',
                week_id
            )
        logger.info(f"Deleted feed sessions for week {week_id} (keep_completed={keep_completed}): {deleted}")


async def get_feed_history(chat_id: int, limit: int = 20) -> List[dict]:
    """Получить историю сессий Ленты"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT s.topic_title, s.fixation_text, s.session_date, s.completed_at
            FROM feed_sessions s
            JOIN feed_weeks w ON s.week_id = w.id
            WHERE w.chat_id = $1 AND s.status = 'completed'
            ORDER BY s.completed_at DESC
            LIMIT $2
        ''', chat_id, limit)

        return [dict(row) for row in rows]
