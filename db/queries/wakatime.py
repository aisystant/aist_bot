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
