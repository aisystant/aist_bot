"""Migration 043: at-most-once receipts for WP-117 milestone nudges.

The receipt and ``development.notification_queue`` share one database so a
producer can claim a milestone and enqueue it in the same transaction.
``recipient_chat_id`` is intentionally explicit: the current delivery contract
has no canonical account key until WP-117 Ф-identity is completed.
"""

from __future__ import annotations

import asyncio

import asyncpg


DDL = """
CREATE TABLE IF NOT EXISTS development.nudge_receipt (
    id BIGSERIAL PRIMARY KEY,
    recipient_chat_id BIGINT NOT NULL,
    nudge_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'delivered')),
    queue_id INTEGER REFERENCES development.notification_queue(id)
        ON DELETE SET NULL,
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    UNIQUE (recipient_chat_id, nudge_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_nudge_receipt_queue
ON development.nudge_receipt(queue_id)
WHERE queue_id IS NOT NULL;
"""


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        existed = await conn.fetchval(
            """SELECT to_regclass('development.nudge_receipt') IS NOT NULL"""
        )
        await conn.execute(DDL)
    return not bool(existed)


if __name__ == "__main__":
    from config import DATABASE_URL

    async def run() -> None:
        pool = await asyncpg.create_pool(DATABASE_URL)
        created = await migrate_if_needed(pool)
        print(f"Migration 043: {'created' if created else 'already exists'}")
        await pool.close()

    asyncio.run(run())
