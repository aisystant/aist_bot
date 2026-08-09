"""
WP-327 Phase 4: IWE Browser Extension token management.

Хранение: persona.user_integrations (service='iwe_extension').
Токен: 32-byte hex, генерируется один раз, не логируется.
"""
from __future__ import annotations

import secrets

from config import get_logger
from db.connection import get_persona_pool

logger = get_logger(__name__)


async def validate_extension_token(token: str) -> str | None:
    """Проверить X-IWE-Api-Key и вернуть account_id или None."""
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT account_id::text
                FROM public.user_integrations
                WHERE service = 'iwe_extension'
                  AND access_token = $1
                  AND active = TRUE
                """,
                token,
            )
            return row["account_id"] if row else None
    except Exception as exc:
        if "does not exist" in str(exc):
            logger.warning("[iwe_extension] user_integrations not available: %s", exc)
            return None
        raise


async def get_extension_token(account_id: str) -> str | None:
    """Вернуть существующий токен для account_id или None если нет."""
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT access_token
                FROM public.user_integrations
                WHERE service = 'iwe_extension'
                  AND account_id = $1::uuid
                  AND active = TRUE
                """,
                account_id,
            )
            return row["access_token"] if row else None
    except Exception as exc:
        if "does not exist" in str(exc):
            return None
        raise


async def generate_extension_token(account_id: str) -> str | None:
    """Создать (или обновить) IWE Extension токен для account_id.

    Возвращает новый токен (32-byte hex) или None если таблица недоступна.
    """
    token = secrets.token_hex(32)
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.user_integrations
                    (account_id, service, access_token, scope, metadata,
                     connected_at, updated_at, active)
                VALUES ($1::uuid, 'iwe_extension', $2, 'typing', '{}'::jsonb,
                        NOW(), NOW(), TRUE)
                ON CONFLICT (account_id, service) DO UPDATE SET
                    access_token = $2,
                    updated_at   = NOW(),
                    active       = TRUE
                """,
                account_id,
                token,
            )
        logger.info("[iwe_extension] token issued for account_id=%s", account_id)
    except Exception as exc:
        if "does not exist" in str(exc):
            logger.warning("[iwe_extension] user_integrations not available: %s", exc)
            return None  # не возвращать несохранённый токен
        raise
    return token
