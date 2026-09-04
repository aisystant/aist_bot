"""Backfill WP-117 milestone receipts from delivered notification events.

Default mode is dry-run. Use ``--apply`` only after migration 039 is deployed.
The script intentionally accepts only the three achievement key families;
``agency_high`` is recognition, but not a one-time milestone.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from db.connection import get_learning_pool, get_pool


_EXTERNAL_ID = re.compile(
    r"^notification-nudge:(?P<chat_id>\d+):[^:]+:"
    r"(?P<nudge_key>nudge_(?:sessions|active_days|stage_reached)_\d+)$"
)


@dataclass(frozen=True)
class HistoricalReceipt:
    recipient_chat_id: int
    nudge_key: str
    delivered_at: datetime


def parse_historical_receipt(
    external_id: str, delivered_at: datetime
) -> HistoricalReceipt | None:
    match = _EXTERNAL_ID.fullmatch(external_id)
    if not match:
        return None
    return HistoricalReceipt(
        recipient_chat_id=int(match.group("chat_id")),
        nudge_key=match.group("nudge_key"),
        delivered_at=delivered_at,
    )


async def load_historical_receipts() -> list[HistoricalReceipt]:
    learning_pool = await get_learning_pool()
    async with learning_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT external_id, ingested_at
               FROM public.domain_event
               WHERE source = 'aist-bot'
                 AND event_type = 'notification_sent'
                 AND payload->>'notification_type' = 'nudge'
                 AND (
                   external_id LIKE 'notification-nudge:%:nudge_sessions_%'
                   OR external_id LIKE 'notification-nudge:%:nudge_active_days_%'
                   OR external_id LIKE 'notification-nudge:%:nudge_stage_reached_%'
                 )
               ORDER BY ingested_at"""
        )

    first_delivery: dict[tuple[int, str], HistoricalReceipt] = {}
    for row in rows:
        receipt = parse_historical_receipt(row["external_id"], row["ingested_at"])
        if receipt is None:
            continue
        first_delivery.setdefault(
            (receipt.recipient_chat_id, receipt.nudge_key), receipt
        )
    return list(first_delivery.values())


async def apply_receipts(receipts: list[HistoricalReceipt]) -> int:
    pool = await get_pool()
    inserted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for receipt in receipts:
                row_id = await conn.fetchval(
                    """INSERT INTO development.nudge_receipt
                       (recipient_chat_id, nudge_key, status,
                        reserved_at, delivered_at)
                       VALUES ($1, $2, 'delivered', $3, $3)
                       ON CONFLICT (recipient_chat_id, nudge_key) DO NOTHING
                       RETURNING id""",
                    receipt.recipient_chat_id,
                    receipt.nudge_key,
                    receipt.delivered_at,
                )
                inserted += int(row_id is not None)
    return inserted


async def main(apply: bool) -> None:
    receipts = await load_historical_receipts()
    print(f"Historical milestone receipts: {len(receipts)}")
    if not apply:
        by_key = Counter(receipt.nudge_key for receipt in receipts)
        for nudge_key, count in sorted(by_key.items()):
            print(f"DRY RUN: {nudge_key}={count}")
        return
    inserted = await apply_receipts(receipts)
    print(f"Applied: {inserted}; already present: {len(receipts) - inserted}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write receipts. Without this flag the command is a dry-run.",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
