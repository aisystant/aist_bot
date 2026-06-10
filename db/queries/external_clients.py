"""
DB queries for external client token auth (WP-411 Ф2).

Tables:
    external_auth_codes  — one-time bootstrap codes (TTL 5 min)
    ory_client_tokens    — persistent access+refresh pairs (Fernet encrypted)
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from db.connection import get_pool
from config import get_logger

logger = get_logger(__name__)

AUTH_CODE_TTL_MINUTES = 5


async def create_auth_code(chat_id: int, account_id: str, scope: str = "full") -> str:
    """Creates one-time bootstrap code. Returns code string (UUID)."""
    code = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(minutes=AUTH_CODE_TTL_MINUTES)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO external_auth_codes (code, chat_id, account_id, scope, expires_at)
            VALUES ($1, $2, $3::uuid, $4, $5)
            """,
            code, chat_id, account_id, scope, expires_at,
        )
    return code


async def exchange_auth_code(code: str) -> Optional[dict]:
    """Exchanges one-time code → {chat_id, account_id, scope}. Deletes on success.

    Returns None if code is unknown or expired.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            DELETE FROM external_auth_codes
            WHERE code = $1 AND expires_at > NOW()
            RETURNING chat_id, account_id::text, scope
            """,
            code,
        )
    if row is None:
        return None
    return {"chat_id": row["chat_id"], "account_id": row["account_id"], "scope": row["scope"]}


async def store_client_token(
    account_id: str,
    access_hash: str,
    access_enc: str,
    refresh_hash: str,
    refresh_enc: str,
    scope: str = "full",
    label: str = "Claude Code",
) -> str:
    """Stores encrypted token pair. Returns token id (UUID)."""
    token_id = str(uuid.uuid4())
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ory_client_tokens
              (id, account_id, access_token_hash, access_token_enc,
               refresh_token_hash, refresh_token_enc, scope, client_label)
            VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8)
            """,
            token_id, account_id,
            access_hash, access_enc,
            refresh_hash, refresh_enc,
            scope, label,
        )
    return token_id


async def lookup_client_token(access_hash: str) -> Optional[dict]:
    """Returns {id, account_id, scope} or None if not found / revoked."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, account_id::text, scope
            FROM ory_client_tokens
            WHERE access_token_hash = $1 AND revoked_at IS NULL
            """,
            access_hash,
        )
    if row is None:
        return None
    return {"id": row["id"], "account_id": row["account_id"], "scope": row["scope"]}


async def touch_client_token(token_id: str) -> None:
    """Updates last_used timestamp. Fire-and-forget safe."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ory_client_tokens SET last_used = NOW() WHERE id = $1::uuid",
            token_id,
        )


async def revoke_client_token(token_id: str, account_id: str) -> bool:
    """Revokes token. Validates ownership. Returns True if revoked."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE ory_client_tokens
            SET revoked_at = NOW()
            WHERE id = $1::uuid AND account_id = $2::uuid AND revoked_at IS NULL
            """,
            token_id, account_id,
        )
    return result == "UPDATE 1"


async def list_client_tokens(account_id: str) -> list:
    """Returns active (non-revoked) tokens for an account, newest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, client_label, scope, last_used, created_at
            FROM ory_client_tokens
            WHERE account_id = $1::uuid AND revoked_at IS NULL
            ORDER BY created_at DESC
            """,
            account_id,
        )
    return [dict(r) for r in rows]


async def cleanup_expired_auth_codes() -> None:
    """Removes expired one-time codes. Called from scheduler."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.execute("DELETE FROM external_auth_codes WHERE expires_at < NOW()")
    if deleted != "DELETE 0":
        logger.info("external_auth_codes cleanup: %s", deleted)
