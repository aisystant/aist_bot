from __future__ import annotations

"""
Запросы для работы с Google Calendar подключениями (таблица google_calendar_connections).

WP-253 lift-and-shift (8 мая): хранилище перенесено из bot_data в Neon secrets БД,
таблица переименована google_calendar → google_calendar_connections.
"""

from datetime import datetime
from typing import Optional, Dict, Any

from config import get_logger
from db.connection import get_secrets_pool

logger = get_logger(__name__)


async def get_calendar_connection(chat_id: int) -> Optional[Dict[str, Any]]:
    """Получить Google Calendar подключение пользователя."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM google_calendar_connections WHERE chat_id = $1', chat_id
        )
        if row:
            return dict(row)
        return None


async def save_calendar_connection(
    chat_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    email: str = None,
) -> None:
    """Сохранить или обновить Google Calendar подключение."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO google_calendar_connections (chat_id, access_token, refresh_token, expires_at, email)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id) DO UPDATE SET
                access_token = $2,
                refresh_token = $3,
                expires_at = $4,
                email = COALESCE($5, google_calendar_connections.email),
                updated_at = NOW()
        ''', chat_id, access_token, refresh_token, expires_at, email)
    logger.info(f"Saved Google Calendar connection for user {chat_id}")


async def update_calendar_tokens(
    chat_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
) -> None:
    """Обновить токены после refresh."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            UPDATE google_calendar_connections
            SET access_token = $1, refresh_token = $2, expires_at = $3, updated_at = NOW()
            WHERE chat_id = $4
        ''', access_token, refresh_token, expires_at, chat_id)


async def delete_calendar_connection(chat_id: int) -> None:
    """Удалить Google Calendar подключение (disconnect)."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'DELETE FROM google_calendar_connections WHERE chat_id = $1', chat_id
        )
    logger.info(f"Deleted Google Calendar connection for user {chat_id}")
