from __future__ import annotations

"""
CRUD для public.users — единый identity layer (WP-82 Phase 2).

T0: telegram_id, без ory_id.
T1+: telegram_id + ory_id (заполняется при регистрации в Ory).
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional
from uuid import UUID

from db.connection import get_pool
from helpers.dual_write import post_event

logger = logging.getLogger(__name__)

# WP-392-b1 follow-up (peer-session 2026-06-09-13): миграция на единственного
# писателя persona.ory_identity.traits.tier = WP-270 projection-worker.
# Флаг отключает ВРЕМЕННЫЙ прямой дублёр бота (_sync_tier_to_persona).
# Default: дублёр ВКЛ (поведение без изменений). Установить "1"/"true"/"yes"
# чтобы отдать запись persona.tier исключительно worker'у — только ПОСЛЕ
# прод-верификации его курсора (см. follow-up D4 /health/deep в WP-392-b1).
DISABLE_BOT_TIER_SYNC = os.getenv("DISABLE_BOT_TIER_SYNC", "").lower() in ("1", "true", "yes")


async def get_or_create_user(
    telegram_id: int,
    name: str = '',
    language: str = 'ru',
) -> dict:
    """Получить или создать запись в public.users по telegram_id.

    Вызывается при создании intern (get_intern).
    Returns dict с полями users.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM public.users WHERE telegram_id = $1',
            telegram_id,
        )
        if row:
            return dict(row)

        row = await conn.fetchrow('''
            INSERT INTO public.users (telegram_id, name, language)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id) DO UPDATE SET telegram_id = EXCLUDED.telegram_id
            RETURNING *
        ''', telegram_id, name, language)
        logger.info(f"[Identity] Created user for telegram_id={telegram_id}, id={row['id']}")

        # WP-268 Phase 2 dual-write: новая регистрация через identity layer
        asyncio.create_task(post_event(
            source="aist-bot",
            external_id=f"user-registered-{row['id']}",
            event_type="user_registered",
            schema_version="v1",
            occurred_at=datetime.utcnow(),
            account_id=None,  # T0 — ory_id появится через link_ory
            payload={
                "user_id": str(row['id']),
                "registration_source": "identity_get_or_create",
                "tier": "T0",
                "language": language,
            },
        ))

        return dict(row)


async def get_user_by_telegram(telegram_id: int) -> Optional[dict]:
    """Получить пользователя по telegram_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM public.users WHERE telegram_id = $1',
            telegram_id,
        )
        return dict(row) if row else None


async def get_user_uuid(telegram_id: int) -> Optional[UUID]:
    """Получить UUID пользователя по telegram_id (для log_event)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id FROM public.users WHERE telegram_id = $1',
            telegram_id,
        )
        return row['id'] if row else None


async def link_ory(telegram_id: int, ory_id: str, email: Optional[str] = None) -> bool:
    """Привязать Ory UUID при переходе T0→T1.

    Args:
        telegram_id: Telegram chat_id
        ory_id: UUID из Ory Network
        email: email из Ory (опционально)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('''
            UPDATE public.users
            SET ory_id = $2, email = COALESCE($3, email),
                tier = CASE WHEN tier = 'T0' THEN 'T1' ELSE tier END,
                updated_at = $4
            WHERE telegram_id = $1
        ''', telegram_id, ory_id, email, datetime.utcnow())
        if result != 'UPDATE 0':
            logger.info(f"[Identity] Linked ory_id={ory_id} for telegram_id={telegram_id}")

            # WP-268 Phase 2 dual-write: Ory привязан, T0→T1
            # external_id = ory_id (стабильный, идемпотентный)
            asyncio.create_task(post_event(
                source="aist-bot",
                external_id=f"ory-linked-{ory_id}",
                event_type="ory_linked",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id=ory_id,
                payload={
                    "tier_to": "T1",
                    "email_present": bool(email),
                },
            ))

            return True
        return False


async def update_user_dt(telegram_id: int, dt_user_id: str) -> bool:
    """Обновить dt_user_id в users."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('''
            UPDATE public.users SET dt_user_id = $2, updated_at = $3
            WHERE telegram_id = $1 AND (dt_user_id IS NULL OR dt_user_id != $2)
        ''', telegram_id, dt_user_id, datetime.utcnow())
        if result != 'UPDATE 0':
            # WP-268 Phase 2 dual-write: dt_user_id привязан
            asyncio.create_task(post_event(
                source="aist-bot",
                external_id=f"dt-linked-{dt_user_id}",
                event_type="dt_linked",
                schema_version="v1",
                occurred_at=datetime.utcnow(),
                account_id=str(dt_user_id),  # dt_user_id = Ory UUID (см. CLAUDE.md §12b)
                payload={},
            ))
            return True
        return False


async def update_user_tier(telegram_id: int, tier: str) -> bool:
    """Обновить тир пользователя.

    Бот — authoritative вычислитель тира: пишет public.users.tier (здесь) и
    эмитит событие tier_changed. КАНОНИЧЕСКАЯ проекция tier_changed →
    persona.ory_identity.traits.tier выполняется WP-270 projection-worker
    (правило в reference.public.projection_rules; README worker'а, строка 34).

    _sync_tier_to_persona ниже — ВРЕМЕННЫЙ прямой дублёр того же значения в
    persona, НЕ safety-net: оба писателя выводят tier из одного события и пишут
    одно значение. Расхождение возможно лишь в узком окне «две смены тира подряд
    + отставший persona-курсор worker'а» (тир меняется редко, окно мало).
    Дублёр управляется флагом DISABLE_BOT_TIER_SYNC и подлежит удалению после
    перехода на worker как единственного писателя (WP-392-b1 follow-up #2).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Читаем текущий tier+ory_id ДО апдейта чтобы понять было ли изменение
        prev = await conn.fetchrow(
            'SELECT tier, ory_id FROM public.users WHERE telegram_id = $1',
            telegram_id,
        )
        result = await conn.execute('''
            UPDATE public.users SET tier = $2, updated_at = $3
            WHERE telegram_id = $1
        ''', telegram_id, tier, datetime.utcnow())

        if result != 'UPDATE 0':
            # WP-268 Phase 2 dual-write: tier_changed
            prev_tier = prev['tier'] if prev else None
            ory_id_str = str(prev['ory_id']) if prev and prev.get('ory_id') else None
            now = datetime.utcnow()
            asyncio.create_task(post_event(
                source="aist-bot",
                external_id=f"tier-changed-{telegram_id}-{int(now.timestamp() * 1_000_000_000)}",
                event_type="tier_changed",
                schema_version="v1",
                occurred_at=now,
                account_id=ory_id_str,
                payload={
                    "tier_from": prev_tier,
                    "tier_to": tier,
                },
            ))

            # WP-392 Б1: временный прямой дублёр в persona (канонический писатель —
            # WP-270 worker по tier_changed). Гасится флагом DISABLE_BOT_TIER_SYNC.
            # Первый attempt inline (без задержки) — устраняет race с gateway GET /tier
            # на T4-full-path (detect_ui_tier → _persist_tier — fire-and-forget task,
            # hermes_chat вызывается сразу). При ошибке — fallback на background retry 3×.
            if ory_id_str and not DISABLE_BOT_TIER_SYNC:
                try:
                    await _sync_persona_tier_once(ory_id_str, tier)
                    logger.info("[Identity] persona tier synced inline ory_id=%s tier=%s", ory_id_str, tier)
                except Exception as _exc:
                    logger.warning("[Identity] inline persona sync failed (%s), scheduling background retry", _exc)
                    asyncio.create_task(_sync_tier_to_persona(ory_id_str, tier))

            return True
        return False


_PERSONA_TIER_SQL = """
    UPDATE public.ory_identity
    SET traits = jsonb_set(
        COALESCE(traits, '{}'::jsonb),
        '{tier}',
        to_jsonb($2::text),
        true
    ),
    last_sync = NOW()
    WHERE account_id = $1::uuid
"""


async def _sync_persona_tier_once(ory_id: str, tier: str) -> None:
    """Single attempt to write tier into persona.ory_identity.traits (no delay, no retry)."""
    from db.connection import get_persona_pool
    persona_pool = await get_persona_pool()
    async with persona_pool.acquire() as pconn:
        await pconn.execute(_PERSONA_TIER_SQL, ory_id, tier)


async def _sync_tier_to_persona(ory_id: str, tier: str) -> None:
    """Fire-and-forget: записать tier в persona.ory_identity.traits. Retry 3× с backoff.

    ВРЕМЕННЫЙ дублёр (см. update_user_tier): канонический писатель — WP-270 worker
    по событию tier_changed. Гонка-под-лагом возможна при быстрых сменах тира;
    удалить после миграции на worker как единственного писателя (флаг
    DISABLE_BOT_TIER_SYNC отключает дублёр без удаления кода).
    """
    delays = [0, 5, 30]  # секунды перед 1-й, 2-й, 3-й попыткой
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            await _sync_persona_tier_once(ory_id, tier)
            return
        except Exception as exc:
            if attempt == len(delays):
                logger.error(
                    f"[Identity] _sync_tier_to_persona gave up after {attempt} attempts "
                    f"for ory_id={ory_id}: {exc}"
                )
            else:
                logger.warning(
                    f"[Identity] _sync_tier_to_persona attempt {attempt} failed "
                    f"for ory_id={ory_id}: {exc}. Retrying in {delays[attempt]}s."
                )
