"""
DB-запросы для привязки Aisystant аккаунта (WP-79).

Хранит aisystant_id в public.users для:
- Проверки подписки БР (определяет T2)
- Запросов к Aisystant API (программы, оплата, занятия)
"""

from db.connection import get_pool
from config import get_logger

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


async def remove_aisystant_link(chat_id: int):
    """Удалить привязку Aisystant аккаунта."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''UPDATE public.users
               SET aisystant_id = NULL,
                   aisystant_linked_at = NULL,
                   updated_at = NOW()
               WHERE telegram_id = $1''',
            chat_id,
        )
    logger.info(f"Aisystant unlinked: chat_id={chat_id}")


async def is_aisystant_linked(chat_id: int) -> bool:
    """Проверить, привязан ли Aisystant аккаунт."""
    return await get_aisystant_id(chat_id) is not None
