from __future__ import annotations

"""
Persistence для Ory OAuth tokens (WP-209 Ф0).

Таблица ory_tokens хранит access/refresh токены, чтобы бот мог
вызывать Gateway MCP от имени пользователя.
Паттерн скопирован с dt_tokens.py.

WP-253 lift-and-shift (8 мая): хранилище перенесено из bot_data в Neon secrets БД.
"""

from datetime import datetime
from typing import Dict, List, Optional

from config import get_logger
from db.connection import get_secrets_pool

logger = get_logger(__name__)


async def save_ory_tokens(
    chat_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    ory_id: Optional[str] = None,
) -> None:
    """Сохранить или обновить Ory tokens для пользователя."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO ory_tokens (chat_id, access_token, refresh_token, expires_at, ory_id, updated_at)
               VALUES ($1, $2, $3, $4, $5, NOW())
               ON CONFLICT (chat_id) DO UPDATE SET
                   access_token = $2,
                   refresh_token = $3,
                   expires_at = $4,
                   ory_id = COALESCE($5, ory_tokens.ory_id),
                   updated_at = NOW()''',
            chat_id, access_token, refresh_token, expires_at, ory_id,
        )


async def load_all_ory_tokens() -> List[Dict]:
    """Загрузить все Ory tokens при старте бота.

    Returns:
        Список словарей {chat_id, access_token, refresh_token, expires_at, ory_id}
    """
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT chat_id, access_token, refresh_token, expires_at, ory_id
               FROM ory_tokens
               WHERE refresh_token IS NOT NULL'''
        )
        return [dict(r) for r in rows]


async def delete_ory_tokens(chat_id: int) -> None:
    """Удалить Ory tokens при отключении."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'DELETE FROM ory_tokens WHERE chat_id = $1',
            chat_id,
        )


async def get_expiring_ory_tokens(margin_seconds: int = 600) -> List[Dict]:
    """Получить токены, которые истекают в ближайшие margin_seconds.

    Используется для proactive refresh.
    """
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT chat_id, access_token, refresh_token, expires_at, ory_id
               FROM ory_tokens
               WHERE refresh_token IS NOT NULL
                 AND expires_at < NOW() + INTERVAL '1 second' * $1''',
            margin_seconds,
        )
        return [dict(r) for r in rows]
