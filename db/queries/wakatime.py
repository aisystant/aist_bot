"""
Запросы для работы с WakaTime подключениями (таблица wakatime_connections).
"""

from typing import Optional, Dict, Any

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def get_wakatime_connection(chat_id: int) -> Optional[Dict[str, Any]]:
    """Получить WakaTime подключение пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM wakatime_connections WHERE chat_id = $1', chat_id
        )
        if row:
            return dict(row)
        return None


async def save_wakatime_connection(
    chat_id: int,
    api_key: str,
    wakatime_username: str = None,
) -> None:
    """Сохранить или обновить WakaTime подключение."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO wakatime_connections (chat_id, api_key, wakatime_username)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id) DO UPDATE SET
                api_key = $2,
                wakatime_username = COALESCE($3, wakatime_connections.wakatime_username),
                connected_at = NOW()
        ''', chat_id, api_key, wakatime_username)
    logger.info(f"Saved WakaTime connection for user {chat_id}")


async def delete_wakatime_connection(chat_id: int) -> None:
    """Удалить WakaTime подключение."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'DELETE FROM wakatime_connections WHERE chat_id = $1', chat_id
        )
    logger.info(f"Deleted WakaTime connection for user {chat_id}")


async def sync_wakatime_to_user_integrations(
    chat_id: int,
    api_key: str,
) -> None:
    """Dual write: синхронизировать WakaTime API key в development.user_integrations.

    Activity Hub IWE-адаптер читает токены из user_integrations (WP-109).
    WakaTime API использует api_key через Basic Auth — тот же формат.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id FROM public.users WHERE telegram_id = $1', chat_id
            )
            if not row or not row['id']:
                logger.warning(
                    f"sync_wakatime_to_user_integrations: no user_uuid for chat_id={chat_id}"
                )
                return

            user_uuid = row['id']

            await conn.execute('''
                INSERT INTO development.user_integrations
                    (user_uuid, service, access_token, scope, metadata,
                     connected_at, updated_at, active)
                VALUES ($1, 'wakatime', $2, 'read_stats', '{}', NOW(), NOW(), TRUE)
                ON CONFLICT (user_uuid, service) DO UPDATE SET
                    access_token = $2,
                    updated_at = NOW(),
                    active = TRUE
            ''',
                user_uuid,
                api_key,
            )
            logger.info(f"Synced WakaTime API key to user_integrations for chat_id={chat_id}")
    except Exception as e:
        logger.warning(f"sync_wakatime_to_user_integrations failed: {e}")
