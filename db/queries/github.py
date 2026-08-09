from __future__ import annotations

"""
Запросы для работы с GitHub подключениями (таблица github_connections).

WP-253 Пробел C: хранилище перенесено из bot_data в Neon secrets БД.
access_token хранится в зашифрованном виде (pgp_sym_encrypt/pgp_sym_decrypt).
Ключ шифрования: GITHUB_TOKEN_ENCRYPTION_KEY (Railway env).
DP.ARCH.004 §B7.3.1: OAuth tokens = secrets ∩ PII → pgcrypto + RLS.
"""

from typing import Optional, Dict, Any

from config import get_logger, GITHUB_TOKEN_ENCRYPTION_KEY
from db.connection import get_secrets_pool

logger = get_logger(__name__)


def _warn_if_no_key() -> None:
    if not GITHUB_TOKEN_ENCRYPTION_KEY:
        logger.warning("GITHUB_TOKEN_ENCRYPTION_KEY не установлен — токены хранятся в незашифрованном виде")


async def get_github_connection(chat_id: int) -> Optional[Dict[str, Any]]:
    """Получить GitHub подключение пользователя (с расшифровкой токена).

    Возвращает None если ключ не установлен — лучше явный сбой,
    чем неполная строка без access_token.
    """
    if not GITHUB_TOKEN_ENCRYPTION_KEY:
        logger.error(
            "GITHUB_TOKEN_ENCRYPTION_KEY не установлен — get_github_connection недоступен"
        )
        return None
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                user_uuid, chat_id,
                pgp_sym_decrypt(access_token_encrypted, $2)::text AS access_token,
                token_type, scope, github_username,
                target_repo, notes_path, strategy_repo, knowledge_repo,
                default_branch, strategy_default_branch,
                created_at, updated_at
            FROM public.github_connections
            WHERE chat_id = $1
        ''', chat_id, GITHUB_TOKEN_ENCRYPTION_KEY)
        if row:
            return dict(row)
        return None


async def save_github_connection(
    chat_id: int,
    access_token: str,
    token_type: str = 'bearer',
    scope: str = None,
    github_username: str = None,
    user_uuid=None,
) -> None:
    """Сохранить или обновить GitHub подключение (с шифрованием токена).

    user_uuid: Ory UUID пользователя. Если None — пытаемся получить из ory_identity.
    """
    _warn_if_no_key()
    if user_uuid is None:
        user_uuid = await _get_user_uuid_by_chat_id(chat_id)

    if user_uuid is None:
        # GitHub OAuth callback требует Ory linkage: без user_uuid не сохраняем.
        # Это не должно происходить в нормальном flow — Ory link обязателен ДО GitHub OAuth.
        raise RuntimeError(
            f"save_github_connection: no Ory user_uuid for chat_id={chat_id}; user must be linked to Ory first"
        )

    # ON CONFLICT (user_uuid): при смене Telegram-аккаунта тот же Ory-пользователь обновит свой chat_id.
    # Это корректнее чем ON CONFLICT (chat_id), который ломается при коллизии user_uuid.
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        if GITHUB_TOKEN_ENCRYPTION_KEY:
            await conn.execute('''
                INSERT INTO public.github_connections
                    (user_uuid, chat_id, access_token_encrypted, token_type, scope, github_username)
                VALUES (
                    $1, $2,
                    pgp_sym_encrypt($3, $6),
                    $4, $5, $7
                )
                ON CONFLICT (user_uuid) DO UPDATE SET
                    chat_id = EXCLUDED.chat_id,
                    access_token_encrypted = pgp_sym_encrypt($3, $6),
                    token_type = $4,
                    scope = $5,
                    github_username = COALESCE($7, github_connections.github_username),
                    updated_at = NOW()
            ''', user_uuid, chat_id, access_token, token_type, scope,
                GITHUB_TOKEN_ENCRYPTION_KEY, github_username)
        else:
            logger.warning(f"save_github_connection: no encryption key, storing placeholder for chat_id={chat_id}")
            await conn.execute('''
                INSERT INTO public.github_connections
                    (user_uuid, chat_id, access_token_encrypted, token_type, scope, github_username)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_uuid) DO UPDATE SET
                    chat_id = EXCLUDED.chat_id,
                    access_token_encrypted = $3,
                    token_type = $4,
                    scope = $5,
                    github_username = COALESCE($6, github_connections.github_username),
                    updated_at = NOW()
            ''', user_uuid, chat_id, b'no-key', token_type, scope, github_username)
    logger.info(f"Saved GitHub connection for user {chat_id} (user_uuid={user_uuid})")


async def update_github_repo(chat_id: int, target_repo: str, default_branch: str = "main") -> None:
    """Обновить целевой репозиторий для заметок."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE public.github_connections SET target_repo = $1, default_branch = $3, updated_at = NOW() WHERE chat_id = $2',
            target_repo, chat_id, default_branch,
        )


async def update_github_notes_path(chat_id: int, notes_path: str) -> None:
    """Обновить путь к файлу заметок."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE public.github_connections SET notes_path = $1, updated_at = NOW() WHERE chat_id = $2',
            notes_path, chat_id,
        )


async def update_github_strategy_repo(chat_id: int, strategy_repo: str, strategy_default_branch: str = "main") -> None:
    """Обновить репозиторий стратега."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE public.github_connections SET strategy_repo = $1, strategy_default_branch = $3, updated_at = NOW() WHERE chat_id = $2',
            strategy_repo, chat_id, strategy_default_branch,
        )


async def update_github_knowledge_repo(chat_id: int, knowledge_repo: str) -> None:
    """Обновить репозиторий индекса знаний (для Публикатора)."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE public.github_connections SET knowledge_repo = $1, updated_at = NOW() WHERE chat_id = $2',
            knowledge_repo, chat_id,
        )


async def get_users_with_knowledge_repo() -> list[dict]:
    """Получить всех пользователей с настроенным knowledge_repo (с расшифровкой токена).

    Возвращает [] если ключ не установлен — без access_token строки бесполезны.
    """
    if not GITHUB_TOKEN_ENCRYPTION_KEY:
        logger.error(
            "GITHUB_TOKEN_ENCRYPTION_KEY не установлен — get_users_with_knowledge_repo недоступен"
        )
        return []
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT
                chat_id,
                pgp_sym_decrypt(access_token_encrypted, $1)::text AS access_token,
                knowledge_repo
            FROM public.github_connections
            WHERE knowledge_repo IS NOT NULL
        ''', GITHUB_TOKEN_ENCRYPTION_KEY)
        return [dict(r) for r in rows]


async def get_users_needing_github_relink() -> list[dict]:
    """Users with active GitHub in user_integrations but missing from secrets.github_connections.

    Cross-DB query (persona + secrets): Python-side set diff.
    Used by scheduler to send one-time relink notification after Gap C cutover.
    """
    from db.connection import get_persona_pool

    # Step 1: chat_ids already linked in secrets (no notification needed)
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        linked = {
            row["chat_id"]
            for row in await conn.fetch(
                "SELECT chat_id FROM public.github_connections WHERE chat_id IS NOT NULL"
            )
        }

    # Step 2: users with github in persona.user_integrations + their telegram chat_id
    persona_pool = await get_persona_pool()
    async with persona_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT oi.telegram_id AS chat_id, ui.account_id
            FROM public.user_integrations ui
            JOIN public.ory_identity oi ON oi.account_id = ui.account_id
            WHERE ui.service = 'github' AND ui.active = TRUE
              AND oi.telegram_id IS NOT NULL
            """
        )

    return [dict(r) for r in rows if r["chat_id"] not in linked]


async def delete_github_connection(chat_id: int) -> None:
    """Удалить GitHub подключение (disconnect)."""
    pool = await get_secrets_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'DELETE FROM public.github_connections WHERE chat_id = $1', chat_id
        )
    logger.info(f"Deleted GitHub connection for user {chat_id}")


async def sync_github_to_user_integrations(
    chat_id: int,
    access_token: str,
    github_username: str,
    scope: str = 'repo',
) -> None:
    """Синхронизировать GitHub-токен в persona.user_integrations.

    Activity Hub IWE-адаптер читает токены из user_integrations.
    Вызывается при GitHub OAuth callback.
    """
    from db.connection import get_persona_pool
    import json as _json

    pool = await get_persona_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT account_id FROM public.ory_identity WHERE telegram_id = $1', chat_id
        )
        if not row or not row['account_id']:
            logger.warning(
                f"sync_github_to_user_integrations: no account_id in ory_identity for chat_id={chat_id}, skipping"
            )
            return

        account_id = row['account_id']

        # WP-253 Gap 1 fix: encrypt token before writing to user_integrations.
        # Format: 'pgp:' + base64(pgp_sym_encrypt(token, key)).
        # Legacy plaintext rows (no 'pgp:' prefix) remain readable by Activity Hub during migration.
        if GITHUB_TOKEN_ENCRYPTION_KEY:
            stored_token = await conn.fetchval(
                "SELECT 'pgp:' || encode(pgp_sym_encrypt($1, $2), 'base64')",
                access_token, GITHUB_TOKEN_ENCRYPTION_KEY,
            )
        else:
            stored_token = access_token

        await conn.execute('''
            INSERT INTO public.user_integrations
                (account_id, service, access_token, scope, metadata, connected_at, updated_at, active)
            VALUES ($1, 'github', $2, $3, $4, NOW(), NOW(), TRUE)
            ON CONFLICT (account_id, service) DO UPDATE SET
                access_token = $2,
                scope = $3,
                metadata = $4,
                updated_at = NOW(),
                active = TRUE
        ''',
            account_id,
            stored_token,
            scope,
            _json.dumps({"github_username": github_username}),
        )
        logger.info(f"Synced GitHub token to persona.user_integrations for chat_id={chat_id}")


async def _get_user_uuid_by_chat_id(chat_id: int):
    """Получить Ory UUID по telegram chat_id из persona.ory_identity."""
    try:
        from db.connection import get_persona_pool
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT account_id FROM public.ory_identity WHERE telegram_id = $1', chat_id
            )
            if row and row['account_id']:
                return row['account_id']
    except Exception as e:
        logger.warning(f"_get_user_uuid_by_chat_id: failed to get UUID for chat_id={chat_id}: {e}")
    return None
