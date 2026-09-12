from __future__ import annotations

"""
WP-567 Ф3(в): transactional outbox for critical bot events (currently
payment_received, subscription_granted from Stars subscription payments).

See db/migrations/049_wp567_event_outbox.py for the schema and the full
rationale: the two events this protects previously left the handler as
`asyncio.create_task(post_event(...))`, a fire-and-forget HTTP call with no
durability if the process died before the task ran. `insert_outbox_row`
writes the row inside the SAME transaction as the business data it
describes (see db/queries/subscription.py `save_subscription_with_outbox`)
-- the row's existence IS the durability guarantee, not a follow-up step
that could itself be skipped.

Deliberately scoped to these two event types only, not every event the bot
emits (peer-session 2026-09-12-09-wp567-stars-retry-parnaya-zapis, Kimi's
narrowing of Codex's outbox proposal) -- non-critical events keep the
existing fire-and-forget `post_event` path.
"""

import json
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def insert_outbox_row(
    conn: asyncpg.Connection,
    external_id: str,
    event_type: str,
    account_id: Optional[str],
    payload: dict,
    occurred_at: datetime,
) -> None:
    """Insert one outbox row within the caller's transaction. Idempotent by
    `external_id` -- ON CONFLICT DO NOTHING lets a caller safely retry the
    whole transaction (e.g. after a Telegram-redelivered update) without
    creating a duplicate outbox row."""
    if occurred_at.tzinfo is not None:
        # column is `TIMESTAMP` (no tz) -- bot convention is naive UTC
        # (helpers/dual_write.py `_to_iso_utc`); asyncpg rejects an
        # aware datetime against a naive column outright (cold review,
        # 2026-09-12: this crashed every call before the fix).
        occurred_at = occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
    await conn.execute(
        """INSERT INTO public.event_outbox
               (external_id, event_type, account_id, payload, occurred_at)
           VALUES ($1, $2, $3, $4::jsonb, $5)
           ON CONFLICT (external_id) DO NOTHING""",
        external_id, event_type, account_id, json.dumps(payload), occurred_at,
    )


async def fetch_pending_outbox(conn: asyncpg.Connection, batch: int) -> list[asyncpg.Record]:
    """Lock and return up to `batch` undelivered rows, oldest first. Caller
    must already be inside `conn.transaction()` -- FOR UPDATE SKIP LOCKED
    only serializes concurrent dispatchers within an explicit transaction
    (same reasoning as core/notification_service.py `drain()`)."""
    return await conn.fetch(
        """SELECT id, external_id, event_type, account_id, payload, occurred_at, attempts
           FROM public.event_outbox
           WHERE delivered_at IS NULL
           ORDER BY created_at ASC
           LIMIT $1
           FOR UPDATE SKIP LOCKED""",
        batch,
    )


async def mark_delivered(conn: asyncpg.Connection, row_id: int) -> None:
    await conn.execute(
        "UPDATE public.event_outbox SET delivered_at = NOW() WHERE id = $1",
        row_id,
    )


async def mark_failed(conn: asyncpg.Connection, row_id: int, error: str) -> None:
    await conn.execute(
        """UPDATE public.event_outbox
           SET attempts = attempts + 1, last_error = $2
           WHERE id = $1""",
        row_id, error[:500],
    )


async def count_stuck_outbox(older_than_minutes: int) -> int:
    """Best-effort count of rows that have sat undelivered longer than
    `older_than_minutes` -- for a watchdog alert, mirroring
    core/scheduler.py `_watch_delivery_queue`. Fail-open: a DB error here
    returns 0 rather than raising, this is a monitor, not the delivery path."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """SELECT count(*) FROM public.event_outbox
                   WHERE delivered_at IS NULL
                     AND created_at < NOW() - ($1 || ' minutes')::interval""",
                str(older_than_minutes),
            )
    except Exception as exc:
        logger.warning(f"[EventOutbox] count_stuck_outbox fail-open: {exc}")
        return 0
