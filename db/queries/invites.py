"""Persistent referral links for WP-266.

The link records attribution only. It never creates a subscription contract or
changes the recipient's access tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from db.connection import get_rewards_pool, get_subscription_pool


@dataclass(frozen=True)
class InviteLink:
    code: str
    link_id: UUID


@dataclass(frozen=True)
class InviteAttribution:
    attribution_id: UUID
    granter_account_id: UUID


class InviteError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _business_error(exc: asyncpg.PostgresError) -> InviteError:
    message = str(exc).splitlines()[0]
    for code in (
        "invite_requires_active_subscription",
        "invite_not_found",
        "invite_self_referral",
        "invite_already_attributed",
    ):
        if code in message:
            return InviteError(code)
    raise exc


async def get_or_create_invite(granter_account_id: str) -> InviteLink:
    pool = await get_subscription_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM public.get_or_create_referral_link($1::uuid)",
                UUID(granter_account_id),
            )
    except asyncpg.PostgresError as exc:
        raise _business_error(exc) from exc
    return InviteLink(code=row["invite_code"], link_id=row["link_id"])


async def activate_invite(code: str, recipient_account_id: str) -> InviteAttribution:
    if not code or len(code) > 64:
        raise InviteError("invite_not_found")
    pool = await get_subscription_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM public.activate_referral_link($1, $2::uuid)",
                code,
                UUID(recipient_account_id),
            )
    except asyncpg.PostgresError as exc:
        raise _business_error(exc) from exc
    return InviteAttribution(
        attribution_id=row["attribution_id"],
        granter_account_id=row["granter_account_id"],
    )


async def get_invite_stats(granter_account_id: str) -> dict[str, Any]:
    granter_uuid = UUID(granter_account_id)
    subscription_pool = await get_subscription_pool()
    async with subscription_pool.acquire() as conn:
        link = await conn.fetchrow(
            "SELECT invite_code FROM public.referral_link WHERE granter_account_id=$1 AND active",
            granter_uuid,
        )
        recipients = await conn.fetch(
            """SELECT recipient_account_id, attributed_at
               FROM public.referral_attribution
               WHERE granter_account_id=$1 ORDER BY attributed_at DESC""",
            granter_uuid,
        )

    recipient_ids = [row["recipient_account_id"] for row in recipients]
    rewards_pool = await get_rewards_pool()
    async with rewards_pool.acquire() as conn:
        paid = 0
        if recipient_ids:
            paid = await conn.fetchval(
                "SELECT COUNT(*) FROM public.first_payment_guard WHERE account_id=ANY($1::uuid[])",
                recipient_ids,
            )
        reward = await conn.fetchrow(
            """SELECT COUNT(*) AS rewards_count, COALESCE(SUM(effective), 0) AS bonuses
               FROM public.applied_events
               WHERE account_id=$1 AND event_type='referral_attributed'""",
            granter_uuid,
        )

    return {
        "invite_code": link["invite_code"] if link else None,
        "invited": len(recipients),
        "paid": int(paid or 0),
        "rewards_count": int(reward["rewards_count"] or 0),
        "bonuses": reward["bonuses"],
    }
