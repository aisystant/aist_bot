"""
DB-запросы для привязки Aisystant аккаунта (WP-79).

Хранит aisystant_id в public.users для:
- Проверки подписки БР (определяет T2)
- Запросов к Aisystant API (программы, оплата, занятия)
"""

import asyncio
from datetime import datetime

from db.connection import get_pool
from config import get_logger
from helpers.dual_write import post_event

logger = get_logger(__name__)


async def get_aisystant_id(chat_id: int) -> str | None:
    """Получить aisystant_id по chat_id. None если не привязан."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT aisystant_id FROM public.users WHERE telegram_id = $1',
            chat_id,
        )
        if row and row['aisystant_id']:
            return row['aisystant_id']
        return None


async def save_aisystant_link(chat_id: int, aisystant_id: str):
    """Сохранить привязку Aisystant аккаунта.

    Также пишет маппинг в development.identity_map (lazy write)
    для Activity Hub (WP-109 Ф1). identity_map → crm.identity_links при WP-183.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''UPDATE public.users
               SET aisystant_id = $2,
                   aisystant_linked_at = NOW(),
                   updated_at = NOW()
               WHERE telegram_id = $1''',
            chat_id, aisystant_id,
        )
        # Lazy write в identity_map для Activity Hub (WP-109)
        user_uuid = await conn.fetchval(
            'SELECT id FROM public.users WHERE telegram_id = $1',
            chat_id,
        )
        if user_uuid:
            await conn.execute(
                '''INSERT INTO development.identity_map (source, external_id, user_uuid)
                   VALUES ('lms', $1, $2)
                   ON CONFLICT (source, external_id) DO NOTHING''',
                str(aisystant_id), user_uuid,
            )
    logger.info(f"Aisystant linked: chat_id={chat_id}, aisystant_id={aisystant_id}")

    # WP-268 Phase 2 dual-write: 2 события на одну операцию
    # (a) aisystant_linked — привязка Aisystant аккаунта к нашему user
    # (b) lms_mapping_added — маппинг в identity_map для Activity Hub
    now = datetime.utcnow()
    account_id_str = str(user_uuid) if user_uuid else None

    asyncio.create_task(post_event(
        source="aist-bot",
        external_id=f"aisystant-linked-{chat_id}-{aisystant_id}",
        event_type="aisystant_linked",
        schema_version="v1",
        occurred_at=now,
        account_id=account_id_str,
        payload={
            "aisystant_id": str(aisystant_id),
        },
    ))

    if user_uuid:
        asyncio.create_task(post_event(
            source="aist-bot",
            external_id=f"lms-mapping-{user_uuid}-{aisystant_id}",
            event_type="lms_mapping_added",
            schema_version="v1",
            occurred_at=now,
            account_id=account_id_str,
            payload={
                "source": "lms",
                "external_id": str(aisystant_id),
            },
        ))


async def remove_aisystant_link(chat_id: int):
    """Удалить привязку Aisystant аккаунта."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Берём user_uuid + предыдущий aisystant_id ДО апдейта
        prev = await conn.fetchrow(
            'SELECT id, aisystant_id FROM public.users WHERE telegram_id = $1',
            chat_id,
        )
        await conn.execute(
            '''UPDATE public.users
               SET aisystant_id = NULL,
                   aisystant_linked_at = NULL,
                   updated_at = NOW()
               WHERE telegram_id = $1''',
            chat_id,
        )
    logger.info(f"Aisystant unlinked: chat_id={chat_id}")

    # WP-268 Phase 2 dual-write: aisystant_unlinked
    if prev:
        now = datetime.utcnow()
        account_id_str = str(prev['id']) if prev.get('id') else None
        prev_aisystant_id = prev.get('aisystant_id')
        asyncio.create_task(post_event(
            source="aist-bot",
            external_id=f"aisystant-unlinked-{chat_id}-{int(now.timestamp() * 1_000_000_000)}",
            event_type="aisystant_unlinked",
            schema_version="v1",
            occurred_at=now,
            account_id=account_id_str,
            payload={
                "aisystant_id_was": str(prev_aisystant_id) if prev_aisystant_id else None,
            },
        ))


async def is_aisystant_linked(chat_id: int) -> bool:
    """Проверить, привязан ли Aisystant аккаунт."""
    return await get_aisystant_id(chat_id) is not None
