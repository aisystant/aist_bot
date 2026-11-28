"""Atomic, fenced ownership of one Telegram voice message (WP-384)."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from db.connection import get_pool


class ClaimState(str, Enum):
    CLAIMED = "claimed"
    BUSY = "busy"
    COMPLETED = "completed"


class LeaseLost(RuntimeError):
    """The caller no longer owns this operation; do not perform external actions."""


@dataclass(frozen=True)
class VoiceClaim:
    state: ClaimState
    operation_id: UUID
    owner: UUID | None
    target: dict | None = None
    result: dict | None = None
    created_at: datetime | None = None


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _claim(row, state):
    owner = row["lease_owner"] if state is ClaimState.CLAIMED else None
    return VoiceClaim(
        state,
        row["operation_id"],
        owner,
        _json(row["target"]),
        _json(row["result"]),
        row["created_at"],
    )


async def claim_voice_operation(
    bot_id: int, chat_id: int, message_id: int, lease_seconds=120
) -> VoiceClaim:
    if not isinstance(lease_seconds, int) or not 5 <= lease_seconds <= 600:
        raise ValueError("Invalid voice lease duration")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO development.voice_operation AS operation
                   (operation_id, bot_id, chat_id, original_message_id, status, lease_owner, lease_until)
               VALUES ($1, $2, $3, $4, 'processing', $5,
                       clock_timestamp() + $6 * interval '1 second')
               ON CONFLICT (bot_id, chat_id, original_message_id) DO UPDATE
                   SET status = 'processing', lease_owner = EXCLUDED.lease_owner,
                       lease_until = EXCLUDED.lease_until, attempts = operation.attempts + 1,
                       updated_at = clock_timestamp()
                   WHERE operation.status = 'retryable' OR
                         (operation.status = 'processing' AND operation.lease_until < clock_timestamp())
               RETURNING operation_id, lease_owner, target, result, created_at""",
            uuid4(),
            bot_id,
            chat_id,
            message_id,
            uuid4(),
            lease_seconds,
        )
        if row is not None:
            return _claim(row, ClaimState.CLAIMED)
        row = await conn.fetchrow(
            """SELECT operation_id, lease_owner, target, result, status, created_at
               FROM development.voice_operation
               WHERE bot_id = $1 AND chat_id = $2 AND original_message_id = $3""",
            bot_id,
            chat_id,
            message_id,
        )
    if row is None:
        raise RuntimeError("Voice operation disappeared during claim")
    state = ClaimState.COMPLETED if row["status"] == "completed" else ClaimState.BUSY
    return _claim(row, state)


async def renew_voice_operation(claim: VoiceClaim, lease_seconds=120) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """UPDATE development.voice_operation
               SET lease_until = clock_timestamp() + $3 * interval '1 second', updated_at = clock_timestamp()
               WHERE operation_id = $1 AND lease_owner = $2 AND status = 'processing'
                 AND lease_until > clock_timestamp()
               RETURNING 1""",
                claim.operation_id,
                claim.owner,
                lease_seconds,
            )
        )


async def bind_voice_target(claim: VoiceClaim, target: dict) -> dict:
    if set(target) != {"repo", "path", "branch"} or not all(
        isinstance(v, str) and v for v in target.values()
    ):
        raise ValueError("Invalid note target")
    pool = await get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """UPDATE development.voice_operation
               SET target = COALESCE(target, $3::jsonb), updated_at = clock_timestamp()
               WHERE operation_id = $1 AND lease_owner = $2 AND status = 'processing'
                 AND lease_until > clock_timestamp()
               RETURNING target""",
            claim.operation_id,
            claim.owner,
            json.dumps(target),
        )
    if value is None:
        raise LeaseLost("Voice target cannot be bound without the active lease")
    return _json(value)


async def bind_voice_kind(claim: VoiceClaim, kind: str) -> str:
    """A retry keeps the first routing decision even if recognition varies."""
    if kind not in {"note", "question"}:
        raise ValueError("Invalid voice operation kind")
    pool = await get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """UPDATE development.voice_operation
               SET result = COALESCE(result, jsonb_build_object('kind', $3::text)),
                   updated_at = clock_timestamp()
               WHERE operation_id = $1 AND lease_owner = $2 AND status = 'processing'
                 AND lease_until > clock_timestamp()
               RETURNING result->>'kind'""",
            claim.operation_id,
            claim.owner,
            kind,
        )
    if value is None:
        raise LeaseLost("Voice routing requires the active lease")
    return value


async def bind_voice_note_hash(claim: VoiceClaim, digest: str) -> str:
    """Persist the first complete physical block fingerprint before its PUT."""
    if not re.fullmatch(r"[0-9a-f]{12}", digest):
        raise ValueError("Invalid voice note fingerprint")
    pool = await get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """UPDATE development.voice_operation
               SET result = jsonb_set(result, '{note_hash}',
                       COALESCE(result->'note_hash', to_jsonb($3::text))),
                   updated_at = clock_timestamp()
               WHERE operation_id = $1 AND lease_owner = $2 AND status = 'processing'
                 AND lease_until > clock_timestamp() AND result->>'kind' = 'note'
               RETURNING result->>'note_hash'""",
            claim.operation_id,
            claim.owner,
            digest,
        )
    if value is None:
        raise LeaseLost("Voice fingerprint requires the active note lease")
    return value


async def complete_voice_operation(claim: VoiceClaim, result: dict) -> None:
    # Receipt metadata only: prevent an accidental transcript or audio payload.
    if not set(result) <= {"kind", "url", "undo_ref"} or result.get("kind") not in {
        "note",
        "question",
    }:
        raise ValueError("Invalid voice receipt metadata")
    pool = await get_pool()
    async with pool.acquire() as conn:
        done = await conn.fetchval(
            """UPDATE development.voice_operation
               SET status = 'completed', result = $3::jsonb, lease_owner = NULL,
                   lease_until = NULL, updated_at = clock_timestamp()
               WHERE operation_id = $1 AND lease_owner = $2 AND status = 'processing'
                 AND lease_until > clock_timestamp()
                 AND (result IS NULL OR result->>'kind' = $4)
               RETURNING 1""",
            claim.operation_id,
            claim.owner,
            json.dumps(result),
            result["kind"],
        )
    if not done:
        raise LeaseLost("Voice completion requires the active lease")


async def release_voice_operation(claim: VoiceClaim) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """UPDATE development.voice_operation
               SET status = 'retryable', lease_owner = NULL, lease_until = NULL,
                   updated_at = clock_timestamp()
               WHERE operation_id = $1 AND lease_owner = $2 AND status = 'processing'
                 AND lease_until > clock_timestamp()
               RETURNING 1""",
                claim.operation_id,
                claim.owner,
            )
        )
