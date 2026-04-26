"""
Запись событий в development.user_events (WP-85, DP.ARCH.003, WP-109).

Append-only event log — Layer 1 архитектуры ЦД.
Единая точка записи для всех событий бота.
Fire-and-forget: ошибка записи НЕ ломает основной flow.

Integrity Pipeline (WP-109 Bot Adapter):
- external_id для dedup (ON CONFLICT DO NOTHING)
- Совместимость с Activity Hub (общий unique index на source+external_id)
"""

import logging
import time
from datetime import datetime
from typing import Optional

from helpers.dual_write import post_event

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
    """Записать событие — single-write на event-gateway (WP-268 cut-over).

    WP-268 cut-over: legacy INSERT INTO development.user_events УДАЛЁН.
    Источник истины — event-gateway (permissive schema legacy_bot_event.v1
    accept'ит любой event_type без schema-регистрации).

    Args:
        user_id: chat_id пользователя
        event_type: тип события (session_start, ai_chat, marathon_step, ...)
        payload: произвольные данные события (передаются только keys, не values)
        confidence: сила сигнала 0.0–1.0
        skill_ids: затронутые компетенции (количество, не сами id)
        source: продьюсер ('bot', 'lms', 'club', 'web_app')

    Returns:
        Синтетический event_id (для совместимости с caller'ами, которые ждут int).
        Реальный id будет назначен gateway/проекцией; этот возвращаемый id —
        локальный hash, не используется для join'ов в legacy.

    ⚠️ Caller'ы, которые ЧИТАЮТ user_events (get_user_events, get_event_counts,
    development.engagement view, dt_sync.py 2_6_coding/2_7_iwe aggregation)
    больше не получат записи от бота. Это касается:
    - dt_sync.sync_engagement_to_dt — ЦД получит stale данные после cut-over
    - /analytics dev-команда
    - tier_detector (если читает ai_chat events)
    Все они должны мигрировать на новую БД (Memory.Observed) в WP-269.
    """
    # Резолв user_uuid (read-only, переходный — для account_id обогащения).
    user_uuid = None
    try:
        from db.queries.identity import get_user_uuid
        user_uuid = await get_user_uuid(user_id)
    except Exception:
        pass

    external_id = _make_external_id(user_id, event_type)

    try:
        await post_event(
            source="aist-bot",
            external_id=external_id,  # идемпотентный (timestamp_ns, см. _make_external_id)
            event_type=event_type,
            schema_version="v1",
            occurred_at=datetime.utcnow(),
            account_id=str(user_uuid) if user_uuid else None,
            payload={
                "user_id": str(user_id),
                "source": source,
                "confidence": confidence,
                "skill_count": len(skill_ids) if skill_ids else 0,
                "payload_keys": list(payload.keys()) if payload else [],
            },
        )
        # Синтетический id для backward-compat с caller'ами:
        synthetic_id = abs(hash(external_id)) % (2**31)
        logger.info(f"[Events] {event_type} emitted for {user_id} (synth_id={synthetic_id})")
        return synthetic_id
    except Exception as e:
        logger.warning(f"[Events] Failed to emit {event_type} for {user_id}: {e}")
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
