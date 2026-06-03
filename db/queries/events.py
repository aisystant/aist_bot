from __future__ import annotations

"""
Запись событий в development.user_events (WP-85, DP.ARCH.003, WP-109).

Append-only event log — Layer 1 архитектуры ЦД.
Единая точка записи для всех событий бота.
Fire-and-forget: ошибка записи НЕ ломает основной flow.

Integrity Pipeline (WP-109 Bot Adapter):
- external_id для dedup (ON CONFLICT DO NOTHING)
- Совместимость с Activity Hub (общий unique index на source+external_id)
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Optional

from db.connection import get_pool
from helpers.dual_write import post_event

logger = logging.getLogger(__name__)


# WP-214 Ф10.7: activity_domain для gateway. Mirrors migration 210 CASE + iwe_event_emit.sh map.
# gateway имеет собственный fallback (resolveActivityDomain в db.ts), но явная передача
# позволяет корректно классифицировать новые event_type до следующего деплоя gateway.
# SoT: reference.event_type_domain_map в Neon (миграция 212, WP-214 Ф8).
# Локальная копия для low-latency write при INSERT в legacy user_events.
# Drift-guard: scripts/verify-activity-domain-sync.py в DS-IT-systems/neon-migrations.
_ACTIVITY_DOMAIN_MAP: dict[str, str] = {
    # learning — освоение нового
    "lesson_completed": "learning", "learning_completed": "learning",
    "qualification_granted": "learning", "training_passed": "learning",
    "training_attempt": "learning", "test_passed": "learning",
    "assessment_completed": "learning", "task_submitted": "learning",
    "text_submitted": "learning", "table_submitted": "learning",
    "feed_completed": "learning", "marathon_step": "learning",
    "marathon_task": "learning", "marathon_tasks": "learning",
    "marathon_step_completed": "learning", "marathon_completed": "learning",
    "workbook_push": "learning", "strategy_session_completed": "learning",
    "knowledge_extracted": "learning", "distinction_added": "learning",
    "method_described": "learning", "topic_created": "learning",
    "club_post_created": "learning", "club_topic_created": "learning",
    "tailor_lesson_sent": "learning", "iwe_research": "learning",
    # practice — ОРЗ, IWE, Pack
    "day_plan_opened": "practice", "day_plan_closed": "practice",
    "day_open": "practice", "day_close": "practice",
    "week_plan_created": "practice", "week_plan_closed": "practice",
    "month_plan_closed": "practice", "slot_logged": "practice",
    "pack_updated": "practice", "iwe_session": "practice",
    "wp_created": "practice", "wp_closed": "practice",
    "wp_completed": "practice", "wp_blocked": "practice",
    "pomodoro_completed": "practice", "note_to_capture": "practice",
    "file_edited": "practice", "git_commit": "practice", "git_push": "practice",
    "bot_reflection": "practice", "command_invoked": "practice",
    # work — взаимодействие с инструментом, runtime
    "request_traced": "work", "session_start": "work",
    "ai_chat": "work", "ai_interaction": "work", "qa_query": "work",
    "coding_time": "work", "content_published": "work",
    "comment_created": "work", "commit_created": "work",
    "fmt_commit_merged": "work", "notification_sent": "work",
    "reminder_delivered": "work", "reminder_opened": "work",
    "nudge_sent": "work", "user_updated": "work",
    "tier_changed": "work", "payment_received": "work",
    "onboarding_step": "work", "onboarding_completed": "work",
    "mode_changed": "work", "settings_changed": "work",
    "progress_viewed": "work", "help_viewed": "work",
    "error_shown": "work", "dt_collect_snapshot": "work",
    "dt_view_requested": "work",
}


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

        # WP-253: consent guard — skip logging for users with explicit opt_out
        if user_uuid:
            try:
                from db.queries.consent import get_consent
                consent = await get_consent(str(user_uuid))
                if consent and not consent.opt_in:
                    logger.info(f"[Events] skip {event_type} for {user_id}: tracking consent opt_out")
                    return None
            except Exception as e:
                logger.warning(f"[Events] consent check failed for {user_id}: {e}")
                # fail-open: continue logging on consent check errors

        external_id = _make_external_id(user_id, event_type)

        async with pool.acquire() as conn:
            # Попытка 1: с external_id + dedup (Neon с миграцией Hub)
            try:
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
            except Exception:
                # Fallback: без external_id (pilot DB без миграции Hub)
                row = await conn.fetchrow('''
                    INSERT INTO development.user_events
                        (user_id, event_type, source, payload, confidence,
                         skill_ids, user_uuid)
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                    RETURNING id
                ''',
                    user_id,
                    event_type,
                    source,
                    json.dumps(payload) if payload else '{}',
                    confidence,
                    skill_ids or [],
                    user_uuid,
                )

            event_id = row['id'] if row else None
            if event_id:
                logger.info(f"[Events] {event_type} logged for {user_id} (id={event_id})")

                # WP-268 Phase 2 Phase B: dual-write центрального event log.
                # Gateway имеет permissive schema legacy_bot_event.v1 для всех
                # 36+ event_types бота — пропустит любой event_type без
                # дополнительной schema-регистрации.
                # Только при успешной legacy записи (event_id != None) — иначе
                # dedup-skip раздваивается, легаси и gateway получат разное.
                try:
                    gw_payload: dict = {
                        "user_id": str(user_id),
                        "source": source,
                        "confidence": confidence,
                        "skill_count": len(skill_ids) if skill_ids else 0,
                        "payload_keys": list(payload.keys()) if payload else [],
                    }
                    # WP-214 Ф10.7: явно передаём activity_domain чтобы gateway
                    # записал его в колонку (не только в payload JSONB).
                    domain = _ACTIVITY_DOMAIN_MAP.get(event_type)
                    if domain:
                        gw_payload["activity_domain"] = domain
                    asyncio.create_task(post_event(
                        source="aist-bot",
                        external_id=external_id,  # уже идемпотентный (см. _make_external_id)
                        event_type=event_type,
                        schema_version="v1",
                        occurred_at=datetime.utcnow(),
                        account_id=str(user_uuid) if user_uuid else None,
                        payload=gw_payload,
                    ))
                except Exception as exc:
                    # Никогда не блокировать legacy путь.
                    logger.warning(f"[Events] dual-write task schedule failed: {exc}")
            else:
                logger.debug(f"[Events] {event_type} dedup skip for {user_id}")
            return event_id
    except Exception as e:
        logger.warning(f"[Events] Failed to log {event_type} for {user_id}: {e}")
        return None


async def log_event_with_occurred_at(
    user_id: int,
    event_type: str,
    payload: Optional[dict] = None,
    occurred_at: Optional[datetime] = None,
    confidence: float = 1.0,
    skill_ids: Optional[list] = None,
    source: str = 'bot',
) -> Optional[int]:
    """Записать событие с явным occurred_at (для backfill в прошлое).

    Расширение log_event: если occurred_at задан — INSERT использует его как
    created_at, иначе NOW(). Используется в /onboarding_time wizard (backfill).

    Privacy: event_type='slot_logged', payload.source='self_report_backfill',
    payload.confidence='estimated' сигнализируют о синтетической природе.
    """
    effective_at = occurred_at or datetime.utcnow()

    try:
        pool = await get_pool()
        user_uuid = None
        try:
            from db.queries.identity import get_user_uuid
            user_uuid = await get_user_uuid(user_id)
        except Exception:
            pass

        external_id = _make_external_id(user_id, event_type)

        async with pool.acquire() as conn:
            # Попытка 1: с created_at + external_id + dedup
            try:
                row = await conn.fetchrow('''
                    INSERT INTO development.user_events
                        (user_id, event_type, source, payload, confidence,
                         skill_ids, user_uuid, external_id, created_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
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
                    effective_at,
                )
            except Exception:
                # Fallback: без external_id и явного created_at
                row = await conn.fetchrow('''
                    INSERT INTO development.user_events
                        (user_id, event_type, source, payload, confidence,
                         skill_ids, user_uuid, created_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
                    RETURNING id
                ''',
                    user_id,
                    event_type,
                    source,
                    json.dumps(payload) if payload else '{}',
                    confidence,
                    skill_ids or [],
                    user_uuid,
                    effective_at,
                )

            event_id = row['id'] if row else None
            if event_id:
                logger.info(
                    f"[Events] {event_type} backfill logged for {user_id} "
                    f"(id={event_id}, occurred_at={effective_at.date()})"
                )
                try:
                    gw_payload: dict = {
                        "user_id": str(user_id),
                        "source": source,
                        "confidence": confidence,
                        "skill_count": len(skill_ids) if skill_ids else 0,
                        "payload_keys": list(payload.keys()) if payload else [],
                    }
                    domain = _ACTIVITY_DOMAIN_MAP.get(event_type)
                    if domain:
                        gw_payload["activity_domain"] = domain
                    asyncio.create_task(post_event(
                        source="aist-bot",
                        external_id=external_id,
                        event_type=event_type,
                        schema_version="v1",
                        occurred_at=effective_at,
                        account_id=str(user_uuid) if user_uuid else None,
                        payload=gw_payload,
                    ))
                except Exception as exc:
                    logger.warning(f"[Events] backfill dual-write task failed: {exc}")
            else:
                logger.debug(f"[Events] {event_type} backfill dedup skip for {user_id}")
            return event_id
    except Exception as e:
        logger.warning(f"[Events] Failed to backfill {event_type} for {user_id}: {e}")
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
