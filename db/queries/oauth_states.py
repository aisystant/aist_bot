"""
OAuth pending states — персистентное хранилище state-токенов.

Заменяет in-memory dict во всех OAuth клиентах (GitHub, Linear, Ory).
State переживает редеплой Railway и рестарт процесса.

TTL: 10 минут (600 секунд). Cleanup через scheduler.
"""

from datetime import datetime, timedelta
from typing import Optional

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)

STATE_TTL_SECONDS = 600  # 10 минут


async def save_oauth_state(state: str, provider: str, telegram_user_id: int) -> None:
    """Сохраняет OAuth state в БД."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO oauth_pending_states (state, provider, telegram_user_id, created_at)
               VALUES ($1, $2, $3, NOW())
               ON CONFLICT (state) DO UPDATE SET
                   provider = $2, telegram_user_id = $3, created_at = NOW()''',
            state, provider, telegram_user_id
        )


async def validate_oauth_state(state: str) -> Optional[int]:
    """Проверяет state и возвращает telegram_user_id. Удаляет использованный state."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''DELETE FROM oauth_pending_states
               WHERE state = $1
                 AND created_at > NOW() - INTERVAL '%s seconds'
               RETURNING telegram_user_id''' % STATE_TTL_SECONDS,
            state
        )
    if row:
        return row['telegram_user_id']
    return None


async def cleanup_expired_oauth_states() -> int:
    """Удаляет просроченные states. Возвращает количество удалённых."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            '''DELETE FROM oauth_pending_states
               WHERE created_at < NOW() - INTERVAL '%s seconds' ''' % STATE_TTL_SECONDS
        )
    count = int(result.split()[-1]) if result else 0
    if count > 0:
        logger.info(f"[OAuth] Cleaned up {count} expired states")
    return count
