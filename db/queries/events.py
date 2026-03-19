"""
Запись событий в development.user_events (WP-85, DP.ARCH.003, WP-109).

Append-only event log — Layer 1 архитектуры ЦД.
Единая точка записи для всех событий бота.
Fire-and-forget: ошибка записи НЕ ломает основной flow.

Integrity Pipeline (WP-109 Bot Adapter):
- external_id для dedup (ON CONFLICT DO NOTHING)
- Совместимость с Activity Hub (общий unique index на source+external_id)
"""

import json
import logging
import time
from typing import Optional

from db.connection import get_pool

logger = logging.getLogger(__name__)


def _make_external_id(user_id: int, event_type: str) -> str:
    """Генерация unique external_id для dedup.

    Формат: bot-{user_id}-{event_type}-{timestamp_ns}
    Наносекундная точность гарантирует уникальность при быстрых вызовах.
    """
    return f"bot-{user_id}-{event_type}-{time.time_ns()}"


async def log_event(
    user_id: int,
    event_type: str,
    payload: Optional[dict] = None,
    confidence: float = 1.0,
    skill_ids: Optional[list] = None,
    source: str = 'bot',
) -> Optional[int]:
    """Записать событие в development.user_events через Integrity Pipeline.

    Args:
        user_id: chat_id пользователя (пока без FK на public.users)
        event_type: тип события (session_start, ai_chat, marathon_step, ...)
        payload: произвольные данные события (JSONB)
        confidence: сила сигнала 0.0–1.0 (тест=0.9, самооценка=0.3)
        skill_ids: затронутые компетенции (TEXT[])
        source: продьюсер ('bot', 'lms', 'club', 'web_app')

    Returns:
        id записи или None при ошибке/dedup
    """
    try:
        pool = await get_pool()
        # Resolve user_uuid from public.users (WP-82 Phase 2)
        user_uuid = None
        try:
            from db.queries.identity import get_user_uuid
            user_uuid = await get_user_uuid(user_id)
        except Exception:
            pass

        external_id = _make_external_id(user_id, event_type)

        async with pool.acquire() as conn:
            row = await conn.fetchrow('''
                INSERT INTO development.user_events
                    (user_id, event_type, source, payload, confidence,
                     skill_ids, user_uuid, external_id)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
                ON CONFLICT (source, external_id) WHERE external_id IS NOT NULL
                DO NOTHING
                RETURNING id
            ''',
                user_id,
                event_type,
                source,
                json.dumps(payload) if payload else '{}',
                confidence,
                skill_ids or [],
                user_uuid,
                external_id,
            )
            event_id = row['id'] if row else None
            if event_id:
                logger.info(f"[Events] {event_type} logged for {user_id} (id={event_id})")
            else:
                logger.debug(f"[Events] {event_type} dedup skip for {user_id}")
            return event_id
    except Exception as e:
        logger.warning(f"[Events] Failed to log {event_type} for {user_id}: {e}")
        return None


async def get_user_events(
    user_id: int,
    event_type: Optional[str] = None,
    limit: int = 50,
) -> list:
    """Получить события пользователя (для аналитики и отладки)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if event_type:
            rows = await conn.fetch('''
                SELECT id, event_type, source, payload, confidence, skill_ids, created_at
                FROM development.user_events
                WHERE user_id = $1 AND event_type = $2
                ORDER BY created_at DESC
                LIMIT $3
            ''', user_id, event_type, limit)
        else:
            rows = await conn.fetch('''
                SELECT id, event_type, source, payload, confidence, skill_ids, created_at
                FROM development.user_events
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            ''', user_id, limit)
        return [dict(r) for r in rows]


async def get_event_counts(hours: int = 24) -> dict:
    """Статистика событий за период (для /analytics)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT event_type, COUNT(*) as count
            FROM development.user_events
            WHERE created_at > NOW() - ($1 || ' hours')::INTERVAL
            GROUP BY event_type
            ORDER BY count DESC
        ''', str(hours))
        return {r['event_type']: r['count'] for r in rows}
