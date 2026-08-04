"""Guest Pass persistence for WP-266.

Raw invitation tokens never reach PostgreSQL: only their SHA-256 digest is stored.
Quota, subscription eligibility, self-referral protection and access issuance are
enforced by SECURITY DEFINER functions in the subscription database.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from db.connection import get_subscription_pool


@dataclass(frozen=True)
class GuestPass:
    token: str
    pass_id: UUID
    claim_expires_at: datetime


@dataclass(frozen=True)
class GuestPassActivation:
    pass_id: UUID
    granter_account_id: UUID
    access_valid_to: datetime
    contract_id: UUID


class GuestPassError(RuntimeError):
    """Expected business rejection returned by the subscription database."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _business_error(exc: asyncpg.PostgresError) -> GuestPassError:
    message = str(exc).splitlines()[0]
    known_codes = (
        "guest_pass_requires_active_subscription",
        "guest_pass_open_quota_reached",
        "guest_pass_not_found",
        "guest_pass_not_available",
        "guest_pass_expired",
        "guest_pass_self_referral",
        "guest_pass_already_used",
        "guest_pass_recipient_has_access",
    )
    for code in known_codes:
        if code in message:
            return GuestPassError(code)
    raise exc


async def create_guest_pass(granter_account_id: str) -> GuestPass:
    """Create one claim link for an active subscriber."""
    granter_uuid = UUID(granter_account_id)
    token = secrets.token_urlsafe(18)
    pool = await get_subscription_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM public.issue_guest_pass($1::uuid, $2)",
                granter_uuid,
                _token_hash(token),
            )
    except asyncpg.PostgresError as exc:
        raise _business_error(exc) from exc

    return GuestPass(
        token=token,
        pass_id=row["pass_id"],
        claim_expires_at=row["claim_expires_at"],
    )


async def activate_guest_pass(token: str, recipient_account_id: str) -> GuestPassActivation:
    """Atomically consume a token and issue 14 days of T2 access."""
    if not token or len(token) > 48:
        raise GuestPassError("guest_pass_not_found")

    recipient_uuid = UUID(recipient_account_id)
    pool = await get_subscription_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM public.activate_guest_pass($1, $2::uuid)",
                _token_hash(token),
                recipient_uuid,
            )
    except asyncpg.PostgresError as exc:
        raise _business_error(exc) from exc

    return GuestPassActivation(
        pass_id=row["pass_id"],
        granter_account_id=row["granter_account_id"],
        access_valid_to=row["access_valid_to"],
        contract_id=row["contract_id"],
    )


async def get_guest_pass_metrics() -> dict[str, Any]:
    """Aggregate counters used to evaluate H-282 without exposing invite tokens."""
    pool = await get_subscription_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS issued,
                   COUNT(*) FILTER (WHERE status = 'activated') AS activated,
                   COUNT(DISTINCT granter_account_id) AS inviters
            FROM public.guest_pass
            """
        )
    return dict(row)
