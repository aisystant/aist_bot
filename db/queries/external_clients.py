"""
DB queries for external client token auth (WP-411 Ф2/Ф4).

Tables:
    external_auth_codes  — one-time bootstrap codes (TTL 5 min)
    ory_client_tokens    — persistent access+refresh pairs
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from db.connection import get_secrets_pool as get_pool
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


async def peek_auth_code_chat_id(code: str) -> Optional[int]:
    """SELECT (not DELETE) chat_id from unexpired auth code. Used to pre-compute tier."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT chat_id FROM external_auth_codes WHERE code = $1 AND expires_at > NOW()",
            code,
        )
    return int(row["chat_id"]) if row else None


async def exchange_code_and_store_tokens(
    code: str,
    access_hash: str,
    refresh_hash: str,
    label: str = "Claude Code",
    computed_tier: str = "T1",
) -> Optional[dict]:
    """Atomic: consume bootstrap code + insert token pair in one transaction.

    Returns {token_id, account_id, scope, chat_id} or None if code unknown/expired.
    """
    token_id = str(uuid.uuid4())
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
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
            await conn.execute(
                """
                INSERT INTO ory_client_tokens
                  (id, account_id, access_token_hash, refresh_token_hash,
                   scope, client_label, computed_tier, chat_id)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8)
                """,
                token_id, row["account_id"],
                access_hash, refresh_hash,
                row["scope"], label,
                computed_tier, row["chat_id"],
            )
    return {
        "token_id": token_id,
        "account_id": row["account_id"],
        "scope": row["scope"],
        "chat_id": int(row["chat_id"]),
    }


async def lookup_client_token(access_hash: str) -> Optional[dict]:
    """Returns {id, account_id, scope, computed_tier} or None if not found / revoked."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, account_id::text, scope, computed_tier
            FROM ory_client_tokens
            WHERE access_token_hash = $1 AND revoked_at IS NULL
            """,
            access_hash,
        )
    if row is None:
        return None
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "scope": row["scope"],
        "computed_tier": row["computed_tier"] or "T1",
    }


async def lookup_refresh_token(refresh_hash: str) -> Optional[dict]:
    """Returns {id, account_id, scope, chat_id} for a valid (non-revoked) refresh token."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, account_id::text, scope, chat_id
            FROM ory_client_tokens
            WHERE refresh_token_hash = $1 AND revoked_at IS NULL
            """,
            refresh_hash,
        )
    if row is None:
        return None
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "scope": row["scope"],
        "chat_id": int(row["chat_id"]) if row["chat_id"] else None,
    }


async def refresh_client_token(
    old_refresh_hash: str,
    new_access_hash: str,
    new_refresh_hash: str,
    computed_tier: str = "T1",
) -> bool:
    """Atomic: revoke old token row, insert new row with updated computed_tier.

    Returns True if successful, False if refresh token not found / already revoked.
    """
    new_id = str(uuid.uuid4())
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                """
                UPDATE ory_client_tokens
                SET revoked_at = NOW()
                WHERE refresh_token_hash = $1 AND revoked_at IS NULL
                RETURNING account_id::text, scope, client_label, chat_id
                """,
                old_refresh_hash,
            )
            if old is None:
                return False
            await conn.execute(
                """
                INSERT INTO ory_client_tokens
                  (id, account_id, access_token_hash, refresh_token_hash,
                   scope, client_label, computed_tier, chat_id)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8)
                """,
                new_id, old["account_id"],
                new_access_hash, new_refresh_hash,
                old["scope"], old["client_label"] or "Claude Code",
                computed_tier, old["chat_id"],
            )
    return True


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


async def revoke_all_client_tokens(account_id: str) -> int:
    """Revokes all active tokens for an account (DP.SC.190 Q1 — fail-secure on tier drop below T3).

    Returns count of tokens revoked.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE ory_client_tokens SET revoked_at = NOW() WHERE account_id = $1::uuid AND revoked_at IS NULL",
            account_id,
        )
    return int(result.split(" ")[-1])


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
