from __future__ import annotations

"""
Persistence для Ory OAuth tokens (WP-209 Ф0).

Таблица ory_tokens хранит access/refresh токены, чтобы бот мог
вызывать Gateway MCP от имени пользователя.
Паттерн скопирован с dt_tokens.py.

WP-253 lift-and-shift (8 мая): хранилище перенесено из bot_data в Neon secrets БД.
"""

from datetime import datetime, timedelta
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
               WHERE refresh_token IS NOT NULL AND refresh_token != '''''
        )
        return [dict(r) for r in rows]


async def load_one_ory_token(chat_id: int) -> Optional[Dict]:
    """Загрузить токен одного пользователя из БД.

    Используется в _refresh_single_token (reactive path 401) для recovery,
    когда in-memory cache не содержит токен — например, после перезапуска
    бота если load_tokens_from_db пропустил эту строку, либо при race с
    OAuth-callback в том же процессе (peer-session 2026-06-01-19, Block GTW).

    Returns:
        dict с {chat_id, access_token, refresh_token, expires_at, ory_id} или None
        если строки нет в БД (значит юзер не подключён к Gateway).
    """
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT chat_id, access_token, refresh_token, expires_at, ory_id
               FROM ory_tokens
               WHERE chat_id = $1 AND refresh_token IS NOT NULL''',
            chat_id,
        )
        return dict(row) if row else None


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
               WHERE refresh_token IS NOT NULL AND refresh_token != ''
                 AND expires_at < NOW() + INTERVAL '1 second' * $1''',
            margin_seconds,
        )
        return [dict(r) for r in rows]


async def refresh_ory_token_with_lock(
    chat_id: int,
    refresh_fn,
    fresh_threshold_seconds: int = 60,
):
    """Cross-process safe refresh через SELECT FOR UPDATE + double-check.

    WP-200 follow-up: устраняет race между ботом и iwe-gateway-bridge.py,
    которые оба могут вызвать refresh_token rotation на одной строке ory_tokens.

    Алгоритм:
      1. Открыть транзакцию, взять row-lock через `SELECT ... FOR UPDATE WHERE chat_id = $1`.
      2. Double-check: если `expires_at > now + fresh_threshold_seconds` — другой writer
         уже обновил, возвращаем текущее значение из БД без вызова Ory.
      3. Иначе вызываем `refresh_fn(refresh_token)` (async), получаем новые tokens.
      4. UPDATE строки в той же транзакции.
      5. COMMIT — отпускаем lock.

    refresh_fn: async callable, принимает refresh_token (str), возвращает dict с ключами
        access_token, refresh_token (опционально), expires_in (опционально, секунды).
        Может бросить исключение — транзакция автоматически откатится через async with.

    Returns:
        dict с access_token + expires_at (после refresh или из БД при double-check hit) либо
        None, если строки нет.

    Raises:
        InvalidGrantError или любое исключение от refresh_fn (обработка возложена на caller —
        транзакция отказывается перед тем как exception дойдёт до caller).
    """
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                '''SELECT access_token, refresh_token, expires_at, ory_id
                   FROM ory_tokens
                   WHERE chat_id = $1
                   FOR UPDATE''',
                chat_id,
            )
            if row is None:
                return None

            # Double-check: пока ждали lock, кто-то мог уже обновить
            expires_at = row['expires_at']
            now = datetime.utcnow()
            if expires_at.tzinfo is not None:
                expires_at_naive = expires_at.replace(tzinfo=None)
            else:
                expires_at_naive = expires_at
            if expires_at_naive > now + timedelta(seconds=fresh_threshold_seconds):
                # Уже свежий — возвращаем без HTTP-вызова к Ory.
                return {
                    'access_token': row['access_token'],
                    'refresh_token': row['refresh_token'],
                    'expires_at': expires_at_naive,
                    'ory_id': row['ory_id'],
                    'from_cache': True,
                }

            # Идём в Ory под row-lock — никто другой не сможет конкурентно
            # запросить refresh для этой же строки (бот ↔ bridge race закрыт).
            new_tokens = await refresh_fn(row['refresh_token'])
            if not new_tokens:
                return None

            # WP-200 follow-up H2: `expires_in` отсчитывается от момента ответа Ory,
            # не от начала запроса. При медленном HTTP (3-5с) старый `now` давал
            # `expires_at` на N секунд раньше реального. Пересчитываем после await.
            now_after = datetime.utcnow()
            new_expires_at = now_after + timedelta(
                seconds=new_tokens.get('expires_in', 3600)
            )
            new_access = new_tokens['access_token']
            new_refresh = new_tokens.get('refresh_token') or row['refresh_token']

            await conn.execute(
                '''UPDATE ory_tokens
                   SET access_token = $1, refresh_token = $2,
                       expires_at = $3, updated_at = NOW()
                   WHERE chat_id = $4''',
                new_access, new_refresh, new_expires_at, chat_id,
            )
            return {
                'access_token': new_access,
                'refresh_token': new_refresh,
                'expires_at': new_expires_at,
                'ory_id': row['ory_id'],
                'from_cache': False,
            }
