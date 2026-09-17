from __future__ import annotations

"""
CRUD для public.users — единый identity layer (WP-82 Phase 2).

T0: telegram_id, без ory_id.
T1+: telegram_id + ory_id (заполняется при регистрации в Ory).
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from db.connection import get_pool
from db.queries import bot_profile
from helpers.dual_write import post_event

logger = logging.getLogger(__name__)



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


_LINK_ORY_RETURNING = (
    "UPDATE public.users SET ory_id = $2, email = COALESCE($3, email), "
    "tier = CASE WHEN tier = 'T0' THEN 'T1' ELSE tier END, updated_at = (NOW() AT TIME ZONE 'utc') "
    "WHERE telegram_id = $1 "
    "RETURNING telegram_id AS chat_id, ory_id, updated_at, " + ", ".join(bot_profile.PROFILE_MIRROR_FIELDS)
)


async def link_ory(telegram_id: int, ory_id: str, email: Optional[str] = None) -> bool:
    """Привязать Ory UUID при переходе T0→T1.

    Args:
        telegram_id: Telegram chat_id
        ory_id: UUID из Ory Network
        email: email из Ory (опционально)

    WP-253 Ф12.6 фаза A: это единственная штатная точка T0→T1 для зеркала —
    persona.bot_profile зеркалит только T1+ (нет владельца для RLS self_only
    у T0, peer-session 2026-09-17-07). Ровно в момент, когда ory_id впервые
    появляется, строка становится зеркалируемой. `updated_at` вычисляется
    СЕРВЕРНОЙ стороной ((NOW() AT TIME ZONE 'utc'), как и в update_intern) —
    не клиентским `datetime.utcnow()` — иначе таймстемп фиксируется ДО
    реального коммита и монотонный guard в bot_profile может молча отклонить
    более позднюю по факту запись как «устаревшую» (cold-review этой сессии).
    По той же причине — под тем же per-chat_id локом, что update_intern/
    update_tg_username: без него зеркало отсюда конкурирует с ними за
    порядок записи в persona.bot_profile без всякой сериализации.
    """
    mirror_lock = await bot_profile.chat_lock(telegram_id) if bot_profile.mirror_enabled_for(telegram_id) else None

    async def _write():
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(_LINK_ORY_RETURNING, telegram_id, ory_id, email)

    if mirror_lock is not None:
        async with mirror_lock:
            row = await _write()
            if row is not None:
                await bot_profile.mirror_profile_row(dict(row))
    else:
        row = await _write()
        if row is not None:
            await bot_profile.mirror_profile_row(dict(row))

    if row is not None:
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


async def update_user_tier(telegram_id: int, tier: str) -> bool:
    """Update user tier in public.users and emit tier_changed event.

    Bot is the authoritative tier computer: reads subscription/github/DT state,
    writes public.users.tier, and emits tier_changed.
    Writing traits.tier in persona.ory_identity belongs exclusively to
    user-profile-service (WP-430 Ф3).
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

            return True
        return False


