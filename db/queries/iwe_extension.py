"""
WP-327 Phase 4: IWE Browser Extension token management; WP-384: notes key for Siri (see below).

Хранение: persona.user_integrations (service='iwe_extension').
Токен: 32-byte hex, генерируется один раз, не логируется.
"""
from __future__ import annotations

import hashlib
import secrets

from config import get_logger
from db.connection import get_persona_pool
from db.queries.token_crypto import encrypt_text_token, decrypt_text_token

logger = get_logger(__name__)


async def validate_extension_token(token: str) -> str | None:
    """Проверить X-IWE-Api-Key и вернуть account_id или None.

    WP-554 Б4: access_token теперь хранится зашифрованным (pgp_sym_encrypt,
    недетерминированный шифротекст) — прямое равенство `access_token = $1`
    больше не находит новые строки. Расшифровка сделана в Python, не в самом
    SQL-запросе построчно с try/except: pgp_sym_decrypt бросает жёсткую
    ошибку при несовпадении ключа (например, во время ротации ключа), а
    делать это внутри WHERE означало бы, что одна чужая нерасшифровываемая
    строка обрывает запрос целиком для ЛЮБОГО пользователя — не только
    владельца этой строки (найдено холодным ревью). Legacy plaintext-строки
    (без префикса 'pgp:') матчатся прямым равенством без обращения к ключу.
    """
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT account_id::text AS account_id, access_token
                FROM public.user_integrations
                WHERE service = 'iwe_extension'
                  AND active = TRUE
                """
            )
            for row in rows:
                stored = row["access_token"]
                if stored == token:
                    return row["account_id"]
                if stored and stored.startswith("pgp:"):
                    try:
                        decrypted = await decrypt_text_token(conn, stored)
                    except Exception:
                        logger.warning(
                            "[iwe_extension] не удалось расшифровать строку account_id=%s при проверке токена — пропускаю",
                            row["account_id"],
                        )
                        continue
                    if decrypted == token:
                        return row["account_id"]
            return None
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
            if not row:
                return None
            return await decrypt_text_token(conn, row["access_token"])
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
            stored_token = await encrypt_text_token(conn, token)
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
                stored_token,
            )
        logger.info("[iwe_extension] token issued for account_id=%s", account_id)
    except Exception as exc:
        if "does not exist" in str(exc):
            logger.warning("[iwe_extension] user_integrations not available: %s", exc)
            return None  # не возвращать несохранённый токен
        raise
    return token


# --- Notes key for Siri / iOS Shortcuts (WP-384) -------------------------------
#
# service='iwe_notes_shortcut', scope='notes'. Only a SHA-256 fingerprint of the key is
# stored (access_token = 'sha256:<hex>'): the key cannot be read back from the database,
# it is shown to its owner once, and a request is checked by one equality lookup (the key
# is 256 random bits, so an unsalted hash is enough). Rows written before the key was
# hashed hold an encrypted token ('pgp:...'); they are not valid keys any more.

NOTES_SHORTCUT_SERVICE = "iwe_notes_shortcut"
_FINGERPRINT_PREFIX = "sha256:"


def notes_key_fingerprint(token: str) -> str:
    return _FINGERPRINT_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


async def issue_notes_key(account_id: str) -> str:
    """Create (or replace) the account's notes key; the old key stops working. Returns the key once."""
    token = secrets.token_hex(32)
    pool = await get_persona_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.user_integrations
                (account_id, service, access_token, scope, metadata, connected_at, updated_at, active)
            VALUES ($1::uuid, $2, $3, 'notes', '{}'::jsonb, NOW(), NOW(), TRUE)
            ON CONFLICT (account_id, service) DO UPDATE SET
                access_token = $3,
                scope        = 'notes',
                updated_at   = NOW(),
                active       = TRUE
            """,
            account_id,
            NOTES_SHORTCUT_SERVICE,
            notes_key_fingerprint(token),
        )
    logger.info("[iwe_extension] notes key issued for account_id=%s", account_id)
    return token


async def validate_notes_key(token: str) -> str | None:
    """account_id of the key's owner, or None."""
    pool = await get_persona_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT account_id::text
            FROM public.user_integrations
            WHERE service = $1 AND active = TRUE AND access_token = $2
            """,
            NOTES_SHORTCUT_SERVICE,
            notes_key_fingerprint(token),
        )


async def has_notes_key(account_id: str) -> bool:
    pool = await get_persona_pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            """
            SELECT 1
            FROM public.user_integrations
            WHERE service = $1 AND account_id = $2::uuid AND active = TRUE AND access_token LIKE $3
            """,
            NOTES_SHORTCUT_SERVICE,
            account_id,
            _FINGERPRINT_PREFIX + "%",
        ))


async def revoke_notes_key(account_id: str) -> bool:
    """Switch the notes key off. True when there was an active key."""
    pool = await get_persona_pool()
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            """
            UPDATE public.user_integrations
            SET active = FALSE, updated_at = NOW()
            WHERE service = $1 AND account_id = $2::uuid AND active = TRUE
            RETURNING 1
            """,
            NOTES_SHORTCUT_SERVICE,
            account_id,
        )
    return bool(updated)


async def get_telegram_id_for_account(account_id: str) -> int | None:
    """Telegram chat of the key owner (persona.ory_identity), or None when not linked."""
    pool = await get_persona_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT telegram_id FROM public.ory_identity WHERE account_id = $1::uuid",
            account_id,
        )
