"""
Запросы для работы с GitHub подключениями (таблица github_connections).
"""

from typing import Optional, Dict, Any

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def get_github_connection(chat_id: int) -> Optional[Dict[str, Any]]:
    """Получить GitHub подключение пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM github_connections WHERE chat_id = $1', chat_id
        )
        if row:
            return dict(row)
        return None


async def save_github_connection(
    chat_id: int,
    access_token: str,
    token_type: str = 'bearer',
    scope: str = None,
    github_username: str = None,
) -> None:
    """Сохранить или обновить GitHub подключение."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO github_connections (chat_id, access_token, token_type, scope, github_username)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id) DO UPDATE SET
                access_token = $2,
                token_type = $3,
                scope = $4,
                github_username = COALESCE($5, github_connections.github_username),
                updated_at = NOW()
        ''', chat_id, access_token, token_type, scope, github_username)
    logger.info(f"Saved GitHub connection for user {chat_id}")


async def update_github_repo(chat_id: int, target_repo: str) -> None:
    """Обновить целевой репозиторий для заметок."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE github_connections SET target_repo = $1, updated_at = NOW() WHERE chat_id = $2',
            target_repo, chat_id,
        )


async def update_github_notes_path(chat_id: int, notes_path: str) -> None:
    """Обновить путь к файлу заметок."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE github_connections SET notes_path = $1, updated_at = NOW() WHERE chat_id = $2',
            notes_path, chat_id,
        )


async def update_github_strategy_repo(chat_id: int, strategy_repo: str) -> None:
    """Обновить репозиторий стратега."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE github_connections SET strategy_repo = $1, updated_at = NOW() WHERE chat_id = $2',
            strategy_repo, chat_id,
        )


async def delete_github_connection(chat_id: int) -> None:
    """Удалить GitHub подключение (disconnect)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'DELETE FROM github_connections WHERE chat_id = $1', chat_id
        )
    logger.info(f"Deleted GitHub connection for user {chat_id}")
