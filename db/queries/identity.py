"""
CRUD для public.users — единый identity layer (WP-82 Phase 2).

T0: telegram_id, без ory_id.
T1+: telegram_id + ory_id (заполняется при регистрации в Ory).
"""

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from db.connection import get_pool

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
            return True
        return False


async def update_user_dt(telegram_id: int, dt_user_id: str) -> bool:
    """Обновить dt_user_id в users (синхронизация с interns)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('''
            UPDATE public.users SET dt_user_id = $2, updated_at = $3
            WHERE telegram_id = $1 AND (dt_user_id IS NULL OR dt_user_id != $2)
        ''', telegram_id, dt_user_id, datetime.utcnow())
        return result != 'UPDATE 0'


async def update_user_tier(telegram_id: int, tier: str) -> bool:
    """Обновить тир пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('''
            UPDATE public.users SET tier = $2, updated_at = $3
            WHERE telegram_id = $1
        ''', telegram_id, tier, datetime.utcnow())
        return result != 'UPDATE 0'
