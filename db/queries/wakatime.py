from __future__ import annotations

"""
Запросы для работы с WakaTime подключениями.

Хранилище: persona.user_integrations (service='wakatime').
Адресация: chat_id → persona.ory_identity (telegram_id) → account_id → user_integrations.

Мигрировано из development.user_integrations в 12-BC архитектуру.
"""

import json
from typing import Optional, Dict, Any

from config import get_logger
from db.connection import get_persona_pool

logger = get_logger(__name__)


async def get_wakatime_connection(chat_id: int) -> Optional[Dict[str, Any]]:
    """Получить WakaTime подключение пользователя.

    Возвращает dict с ключами: chat_id, api_key, wakatime_username, connected_at
    (обратная совместимость с потребителями).
    """
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT
                    oi.telegram_id AS chat_id,
                    ui.access_token AS api_key,
                    ui.metadata->>'wakatime_username' AS wakatime_username,
                    ui.connected_at
                FROM public.user_integrations ui
                JOIN public.ory_identity oi ON oi.account_id = ui.account_id
                WHERE oi.telegram_id = $1
                    AND ui.service = 'wakatime'
                    AND ui.active = TRUE
            ''', chat_id)
            if row:
                return dict(row)
            return None
    except Exception as e:
        if 'does not exist' in str(e):
            logger.warning(f"persona.user_integrations not available (pilot?): {e}")
            return None
        raise


async def get_all_wakatime_accounts() -> list[dict]:
    """Все активные WakaTime аккаунты для batch-сбора typing events.

    Возвращает список dict: {account_id, chat_id, api_key}.
    Используется в WP-327 Phase 4 wakatime_typing_collector.
    """
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT
                    ui.account_id,
                    oi.telegram_id AS chat_id,
                    ui.access_token AS api_key
                FROM public.user_integrations ui
                JOIN public.ory_identity oi ON oi.account_id = ui.account_id
                WHERE ui.service = 'wakatime'
                    AND ui.active = TRUE
                    AND oi.telegram_id IS NOT NULL
            ''')
            return [dict(r) for r in rows]
    except Exception as e:
        if 'does not exist' in str(e):
            logger.warning(f"persona.user_integrations not available: {e}")
            return []
        raise


async def save_wakatime_connection(
    chat_id: int,
    api_key: str,
    wakatime_username: str = None,
) -> None:
    """Сохранить или обновить WakaTime подключение в persona.user_integrations."""
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            account_row = await conn.fetchrow(
                'SELECT account_id FROM public.ory_identity WHERE telegram_id = $1', chat_id
            )
            if not account_row or not account_row['account_id']:
                logger.warning(
                    f"save_wakatime_connection: no account_id for chat_id={chat_id}"
                )
                return

            account_id = account_row['account_id']
            metadata = '{}'
            if wakatime_username:
                metadata = json.dumps({"wakatime_username": wakatime_username})

            await conn.execute('''
                INSERT INTO public.user_integrations
                    (account_id, service, access_token, scope, metadata,
                     connected_at, updated_at, active)
                VALUES ($1, 'wakatime', $2, 'read_stats', $3::jsonb, NOW(), NOW(), TRUE)
                ON CONFLICT (account_id, service) DO UPDATE SET
                    access_token = $2,
                    metadata = CASE
                        WHEN $3::jsonb != '{}'::jsonb
                        THEN user_integrations.metadata || $3::jsonb
                        ELSE user_integrations.metadata
                    END,
                    updated_at = NOW(),
                    active = TRUE
            ''', account_id, api_key, metadata)
            logger.info(f"Saved WakaTime connection for chat_id={chat_id}")
    except Exception as e:
        if 'does not exist' in str(e):
            logger.warning(f"persona.user_integrations not available (pilot?): {e}")
        else:
            raise


async def delete_wakatime_connection(chat_id: int) -> None:
    """Отключить WakaTime (soft delete: active=FALSE)."""
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            await conn.execute('''
                UPDATE public.user_integrations
                SET active = FALSE, updated_at = NOW()
                WHERE account_id = (
                    SELECT account_id FROM public.ory_identity WHERE telegram_id = $1
                )
                AND service = 'wakatime'
            ''', chat_id)
        logger.info(f"Disconnected WakaTime for chat_id={chat_id}")
    except Exception as e:
        if 'does not exist' in str(e):
            logger.warning(f"persona.user_integrations not available (pilot?): {e}")
        else:
            raise
