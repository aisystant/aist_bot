"""Запросы для GitHub App installations (WP-301 Ф7).

Хранилище — Neon secrets БД, таблица `public.github_connections`
(дополнительные колонки в migration 014).

Маршрутизация:
- При установке App на репо пилота: save_app_installation(chat_id, installation_id, repo_full_name)
- Webhook handler: find_user_by_installation_id(installation_id) → chat_id, dt_user_id
- Платформа пишет assignments: get_app_installation(chat_id) → installation_id, repo_full_name
"""

from __future__ import annotations

from typing import Optional, Dict, Any

from config import get_logger
from db.connection import get_secrets_pool

logger = get_logger(__name__)


async def save_app_installation(
    chat_id: int,
    installation_id: int,
    repo_full_name: str,
    github_username: str,
) -> tuple[bool, str]:
    """Сохранить факт установки App на пилотском репо.

    Схема `github_connections` (WP-253 030-secrets-github-connections.sql):
    - user_uuid UUID PRIMARY KEY (NOT NULL)
    - chat_id BIGINT UNIQUE NOT NULL
    - access_token_encrypted BYTEA NOT NULL

    Два пути:
    1. **Запись уже есть** (пилот ранее делал /github OAuth) → UPDATE app-колонок по chat_id
    2. **Записи нет** → нужен user_uuid из dt_tokens (Ory login). Если есть — INSERT
       с пустым access_token_encrypted (App работает независимо от OAuth). Если нет —
       возвращаем ошибку с инструкцией пилоту: сначала /start (Ory onboarding).

    Returns (success, error_message_or_status).
    """
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        try:
            # Path 1: UPDATE если запись с таким chat_id есть
            updated = await conn.fetchval(
                """
                UPDATE public.github_connections
                SET app_installation_id = $2,
                    app_repo_full_name = $3,
                    app_installed_at = NOW(),
                    app_suspended = FALSE,
                    github_username = COALESCE(github_username, $4),
                    updated_at = NOW()
                WHERE chat_id = $1
                RETURNING user_uuid
                """,
                chat_id, installation_id, repo_full_name, github_username,
            )
            if updated:
                logger.info(
                    "[GitHubApp] updated installation: chat_id=%d, installation_id=%d, repo=%s",
                    chat_id, installation_id, repo_full_name,
                )
                return True, "updated"

            # Path 2: INSERT — нужен user_uuid из dt_tokens
            dt_row = await conn.fetchrow(
                "SELECT dt_user_id FROM public.dt_tokens WHERE chat_id = $1 LIMIT 1",
                chat_id,
            )
            if not dt_row or not dt_row.get("dt_user_id"):
                logger.warning(
                    "[GitHubApp] save_app_installation: no dt_tokens for chat_id=%d (Ory login required)",
                    chat_id,
                )
                return False, "ory_login_required"

            user_uuid = dt_row["dt_user_id"]
            # INSERT с пустым access_token_encrypted (App-only path, OAuth не требуется)
            await conn.execute(
                """
                INSERT INTO public.github_connections
                    (user_uuid, chat_id, access_token_encrypted, github_username,
                     app_installation_id, app_repo_full_name, app_installed_at, app_suspended)
                VALUES ($1, $2, ''::BYTEA, $3, $4, $5, NOW(), FALSE)
                ON CONFLICT (user_uuid) DO UPDATE
                SET app_installation_id = EXCLUDED.app_installation_id,
                    app_repo_full_name = EXCLUDED.app_repo_full_name,
                    app_installed_at = NOW(),
                    app_suspended = FALSE,
                    github_username = COALESCE(public.github_connections.github_username,
                                               EXCLUDED.github_username),
                    updated_at = NOW()
                """,
                user_uuid, chat_id, github_username, installation_id, repo_full_name,
            )
            logger.info(
                "[GitHubApp] inserted installation: chat_id=%d, user_uuid=%s, installation_id=%d",
                chat_id, user_uuid, installation_id,
            )
            return True, "inserted"
        except Exception as e:
            logger.error("[GitHubApp] save_app_installation failed: %s", e)
            return False, f"db_error: {e}"


async def get_app_installation(chat_id: int) -> Optional[Dict[str, Any]]:
    """Получить installation_id + repo для пилота. None если App не установлен."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT chat_id, github_username,
                   app_installation_id, app_repo_full_name,
                   app_installed_at, app_suspended
            FROM public.github_connections
            WHERE chat_id = $1 AND app_installation_id IS NOT NULL
            """,
            chat_id,
        )
        return dict(row) if row else None


async def find_user_by_installation_id(installation_id: int) -> Optional[Dict[str, Any]]:
    """Reverse lookup: installation_id → chat_id / github_username.

    Используется webhook handler'ом: GitHub присылает installation_id в payload,
    нужно найти, какому пользователю это принадлежит.
    """
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT chat_id, github_username, app_repo_full_name, app_suspended
            FROM public.github_connections
            WHERE app_installation_id = $1
            """,
            installation_id,
        )
        return dict(row) if row else None


async def mark_installation_suspended(installation_id: int, suspended: bool = True) -> bool:
    """Отметить установку как suspended (uninstall flow / user revoked App)."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        try:
            result = await conn.execute(
                """
                UPDATE public.github_connections
                SET app_suspended = $2, updated_at = NOW()
                WHERE app_installation_id = $1
                """,
                installation_id, suspended,
            )
            logger.info(
                "[GitHubApp] installation %d suspended=%s (result=%s)",
                installation_id, suspended, result,
            )
            return True
        except Exception as e:
            logger.error("[GitHubApp] mark_installation_suspended failed: %s", e)
            return False


async def delete_installation(installation_id: int) -> bool:
    """Hard delete installation (App removed entirely). Очищает только App-колонки,
    OAuth connection (если был) остаётся."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                UPDATE public.github_connections
                SET app_installation_id = NULL,
                    app_repo_full_name = NULL,
                    app_installed_at = NULL,
                    app_suspended = FALSE,
                    updated_at = NOW()
                WHERE app_installation_id = $1
                """,
                installation_id,
            )
            logger.info("[GitHubApp] installation %d deleted", installation_id)
            return True
        except Exception as e:
            logger.error("[GitHubApp] delete_installation failed: %s", e)
            return False
