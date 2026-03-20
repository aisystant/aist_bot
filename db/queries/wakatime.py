"""
Запросы для работы с WakaTime подключениями.

Единственное хранилище: development.user_integrations (service='wakatime').
Адресация: chat_id → JOIN public.users → user_uuid → user_integrations.

Миграция WP-109/WP-7: wakatime_connections удалена, все операции через user_integrations.
"""

import json
from typing import Optional, Dict, Any

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def get_wakatime_connection(chat_id: int) -> Optional[Dict[str, Any]]:
    """Получить WakaTime подключение пользователя.

    Возвращает dict с ключами: chat_id, api_key, wakatime_username, connected_at
    (обратная совместимость с потребителями).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                u.telegram_id AS chat_id,
                ui.access_token AS api_key,
                ui.metadata->>'wakatime_username' AS wakatime_username,
                ui.connected_at
            FROM development.user_integrations ui
            JOIN public.users u ON u.id = ui.user_uuid
            WHERE u.telegram_id = $1
                AND ui.service = 'wakatime'
                AND ui.active = TRUE
        ''', chat_id)
        if row:
            return dict(row)
        return None


async def save_wakatime_connection(
    chat_id: int,
    api_key: str,
    wakatime_username: str = None,
) -> None:
    """Сохранить или обновить WakaTime подключение в user_integrations."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Резолвим user_uuid
        user_row = await conn.fetchrow(
            'SELECT id FROM public.users WHERE telegram_id = $1', chat_id
        )
        if not user_row or not user_row['id']:
            logger.warning(
                f"save_wakatime_connection: no user_uuid for chat_id={chat_id}"
            )
            return

        user_uuid = user_row['id']
        metadata = '{}'
        if wakatime_username:
            metadata = json.dumps({"wakatime_username": wakatime_username})

        await conn.execute('''
            INSERT INTO development.user_integrations
                (user_uuid, service, access_token, scope, metadata,
                 connected_at, updated_at, active)
            VALUES ($1, 'wakatime', $2, 'read_stats', $3::jsonb, NOW(), NOW(), TRUE)
            ON CONFLICT (user_uuid, service) DO UPDATE SET
                access_token = $2,
                metadata = CASE
                    WHEN $3::jsonb != '{}'::jsonb
                    THEN development.user_integrations.metadata || $3::jsonb
                    ELSE development.user_integrations.metadata
                END,
                updated_at = NOW(),
                active = TRUE
        ''', user_uuid, api_key, metadata)
    logger.info(f"Saved WakaTime connection for chat_id={chat_id}")


async def delete_wakatime_connection(chat_id: int) -> None:
    """Отключить WakaTime (soft delete: active=FALSE)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            UPDATE development.user_integrations
            SET active = FALSE, updated_at = NOW()
            WHERE user_uuid = (SELECT id FROM public.users WHERE telegram_id = $1)
                AND service = 'wakatime'
        ''', chat_id)
    logger.info(f"Disconnected WakaTime for chat_id={chat_id}")
