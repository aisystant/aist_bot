from __future__ import annotations

"""
Persistence для токенов Digital Twin OAuth.

Таблица dt_tokens хранит access/refresh токены, чтобы подключение
к ЦД не терялось при редеплое бота (WP-82, WP-7 D4).

WP-253 lift-and-shift (8 мая): хранилище перенесено из bot_data в Neon secrets БД.
"""

from datetime import datetime
from typing import Dict, List, Optional

from config import get_logger
from db.connection import get_secrets_pool
from db.queries.token_crypto import encrypt_text_token, decrypt_text_token

logger = get_logger(__name__)


async def save_dt_tokens(
    chat_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    dt_user_id: Optional[str] = None,
) -> None:
    """Сохранить или обновить токены ЦД для пользователя."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        stored_access_token = await encrypt_text_token(conn, access_token)
        stored_refresh_token = await encrypt_text_token(conn, refresh_token)
        await conn.execute(
            '''INSERT INTO public.dt_tokens (chat_id, access_token, refresh_token, expires_at, dt_user_id, updated_at)
               VALUES ($1, $2, $3, $4, $5, NOW())
               ON CONFLICT (chat_id) DO UPDATE SET
                   access_token = $2,
                   refresh_token = $3,
                   expires_at = $4,
                   dt_user_id = COALESCE($5, dt_tokens.dt_user_id),
                   updated_at = NOW()''',
            chat_id, stored_access_token, stored_refresh_token, expires_at, dt_user_id,
        )


async def load_all_dt_tokens() -> List[Dict]:
    """Загрузить все токены при старте бота.

    Returns:
        Список словарей {chat_id, access_token, refresh_token, expires_at, dt_user_id}
    """
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT chat_id, access_token, refresh_token, expires_at, dt_user_id
               FROM public.dt_tokens
               WHERE refresh_token IS NOT NULL'''
        )
        results = [dict(r) for r in rows]
        decrypted = []
        for r in results:
            try:
                r['access_token'] = await decrypt_text_token(conn, r['access_token'])
                r['refresh_token'] = await decrypt_text_token(conn, r['refresh_token'])
            except Exception:
                # WP-554 Б4: одна нерасшифровываемая строка (например, ключ
                # уже сменился, а строка ещё под старым) не должна ронять
                # весь стартовый прогрев ЦД-подключений для остальных.
                logger.warning(
                    "load_all_dt_tokens: не удалось расшифровать токены chat_id=%s — пропускаю",
                    r.get('chat_id'),
                )
                continue
            decrypted.append(r)
        return decrypted


async def delete_dt_tokens(chat_id: int) -> None:
    """Удалить токены при отключении от ЦД."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'DELETE FROM public.dt_tokens WHERE chat_id = $1',
            chat_id,
        )


async def get_dt_user_id(chat_id: int) -> Optional[str]:
    """Получить dt_user_id для пользователя."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT dt_user_id FROM public.dt_tokens WHERE chat_id = $1',
            chat_id,
        )
