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

            # At-source dt_user_id sync (peer-session 2026-06-25-09): ory_id and
            # dt_user_id both hold the Ory UUID, but historically only ory_id was set
            # here -> /points (reads dt_user_id) showed "not linked" for OAuth users.
            # Best-effort: linking already succeeded; a transient DB fault here must
            # NOT fail the link (the daily reconcile job is the safety-net).
            try:
                status = await _sync_dt_user_id_from_uuid(conn, telegram_id, ory_id)
                if status == 'conflict':
                    logger.warning(
                        f"[Identity] identity_conflict: ory_id={ory_id} already owned by "
                        f"another telegram_id; dt_user_id NOT set for {telegram_id}")
            except Exception as e:
                logger.warning(
                    f"[Identity] dt_user_id at-source sync failed for {telegram_id}: {e} "
                    f"(reconcile job will retry)")

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


async def _sync_dt_user_id_from_uuid(conn, telegram_id: int, ory_uuid: str) -> str:
    """Set users.dt_user_id = ory_uuid when it is NULL and the uuid is free.

    Returns 'set' | 'present' | 'conflict'. Collision-safe: the NOT EXISTS guard
    skips a uuid already owned by another telegram_id (unique users_dt_user_id_key),
    so it never raises on a duplicate. Source: peer-session 2026-06-25-09.
    """
    res = await conn.execute(
        '''UPDATE public.users SET dt_user_id = $2, updated_at = $3
           WHERE telegram_id = $1 AND dt_user_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM public.users u2 WHERE u2.dt_user_id = $2)''',
        telegram_id, ory_uuid, datetime.utcnow())
    if res != 'UPDATE 0':
        return 'set'
    row = await conn.fetchrow(
        'SELECT dt_user_id FROM public.users WHERE telegram_id = $1', telegram_id)
    return 'present' if (row and row['dt_user_id'] is not None) else 'conflict'


async def reconcile_dt_user_id(limit: int = 1000) -> dict:
    """Daily safety-net: fill users.dt_user_id where NULL, from two sources.

    public.users keeps the Ory UUID in both `ory_id` and `dt_user_id`; some connect
    paths set only `ory_id`, leaving `/points` (reads dt_user_id) broken. This
    reconciles, collision-safe:
    1. same-row `ory_id` -> `dt_user_id` (cheap bulk UPDATE);
    2. `persona.ory_identity.account_id` by telegram_id (cross-DB, for users that
       never went through link_ory, e.g. ETL imports).

    Returns {from_ory, from_persona, conflicts}. Idempotent. Conflicts are logged
    as identity_conflict, never overwritten. Source: peer-session 2026-06-25-09.
    """
    pool = await get_pool()
    counts = {'from_ory': 0, 'from_persona': 0, 'conflicts': 0}
    async with pool.acquire() as conn:
        # ory_id is uuid, dt_user_id is text -> cast to compare/assign.
        res = await conn.execute(
            '''UPDATE public.users u SET dt_user_id = u.ory_id::text, updated_at = $1
               WHERE u.dt_user_id IS NULL AND u.ory_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM public.users u2 WHERE u2.dt_user_id = u.ory_id::text)''',
            datetime.utcnow())
        counts['from_ory'] = int(res.split()[-1]) if res.startswith('UPDATE') else 0
        remaining = [r['telegram_id'] for r in await conn.fetch(
            'SELECT telegram_id FROM public.users WHERE dt_user_id IS NULL LIMIT $1', limit)]

    if not remaining:
        logger.info(f"[Reconcile] dt_user_id from_ory={counts['from_ory']} (persona not needed)")
        return counts

    from db.connection import get_persona_pool
    persona = await get_persona_pool()
    async with persona.acquire() as pconn:
        mapping = {r['telegram_id']: str(r['account_id']) for r in await pconn.fetch(
            '''SELECT telegram_id, account_id FROM public.ory_identity
               WHERE telegram_id = ANY($1::bigint[]) AND account_id IS NOT NULL''',
            remaining)}

    async with pool.acquire() as conn:
        for tg, acc in mapping.items():
            status = await _sync_dt_user_id_from_uuid(conn, tg, acc)
            if status == 'set':
                counts['from_persona'] += 1
            elif status == 'conflict':
                counts['conflicts'] += 1
                logger.warning(f"[Reconcile] identity_conflict tg={tg} acc={acc} already owned")
    logger.info(
        f"[Reconcile] dt_user_id from_ory={counts['from_ory']} "
        f"from_persona={counts['from_persona']} conflicts={counts['conflicts']}")
    return counts


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


