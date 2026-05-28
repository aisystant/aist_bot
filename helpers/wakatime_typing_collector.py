"""
WP-327 Phase 4: server-side WakaTime typing collector.

Подход: вместо VS Code Extension — опрашиваем WakaTime API ежедневно.
WakaTime Extension уже установлена у пользователей; cumulative_total.seconds
конвертируется в chars с консервативным коэффициентом.

Конвертация: 60 chars/min, floor 120s (=2 min heartbeat WakaTime).
channel_weight = 0.5 (medium accuracy: time ≠ typing time).

Идемпотентность: external_id = "wakatime-{account_id}-{date}" (UNIQUE в domain_event).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

logger = logging.getLogger(__name__)

CHARS_PER_SECOND = 1.0      # 60 chars/min = 1 char/sec
MIN_CODING_SECONDS = 120    # floor: 2 min (один WakaTime heartbeat)
CHANNEL_WEIGHT = 0.5        # medium accuracy


async def collect_wakatime_typing() -> None:
    """Основная точка входа — вызывается из scheduler ежедневно в 22:00 UTC."""
    from clients.wakatime import WakaTimeClient
    from db.queries.wakatime import get_all_wakatime_accounts
    from db.queries.consent import is_typing_tracking_disabled
    from helpers.dual_write import post_event
    from datetime import timezone, datetime, timedelta

    accounts = await get_all_wakatime_accounts()
    if not accounts:
        logger.info("[wakatime_collector] no linked accounts")
        return

    logger.info("[wakatime_collector] start: %d accounts", len(accounts))
    client = WakaTimeClient()
    emitted = 0
    # Запрашиваем вчерашний день — он завершён. Cron 22:00 UTC → данные за UTC-день полны.
    today = date.today() - timedelta(days=1)

    for acc in accounts:
        account_id: str = str(acc["account_id"])
        api_key: str = acc["api_key"]

        try:
            if await is_typing_tracking_disabled(account_id):
                continue
        except Exception:
            pass  # fail-open

        try:
            summary = await client.get_day_summary(api_key, day=today)
        except Exception as exc:
            logger.warning("[wakatime_collector] fetch error account=%s: %s", account_id, exc)
            continue

        if not summary:
            continue

        total_seconds: int = summary.get("total_seconds", 0)
        if total_seconds < MIN_CODING_SECONDS:
            continue

        char_count = round(total_seconds * CHARS_PER_SECOND)
        weighted = round(char_count * CHANNEL_WEIGHT, 2)
        date_str = today.isoformat()

        await post_event(
            source="aist-bot",
            external_id=f"wakatime-{account_id}-{date_str}",
            event_type="user_typing_tracked",
            schema_version="v1",
            occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
            account_id=account_id,
            payload={
                "interface": "vscode",
                "date": date_str,
                "coding_seconds": total_seconds,
                "char_count": char_count,
                "weighted_chars": weighted,
                "channel_weight": CHANNEL_WEIGHT,
            },
        )
        emitted += 1
        await asyncio.sleep(0.1)

    logger.info("[wakatime_collector] done: %d events emitted", emitted)
