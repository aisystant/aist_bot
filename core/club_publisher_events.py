"""WP-568 Ф13: club publisher facts for the platform event journal.

The club publisher (P-16, core/scheduler.py) records domain facts about each
author's publications -- scheduled, published, failed, queue low -- as
`club_publication.v1` / `club_publication_queue_low.v1` rows in the durable
event_outbox; `_drain_event_outbox` delivers them to event-gateway. The pilot's
notification center on tsekh-1 reads the pilot's events from the journal and
decides live vs digest; the bot does not decide channels.

Two env switches (config/settings.py):
  CLUB_PUBLISHER_EVENTS_INSTANCE  "" (off) | "pilot" | "prod" -- also the
                                  `bot_instance` field that keeps pilot-bot
                                  events apart from production ones.
  CLUB_PUBLISHER_PILOT_VIA_CENTER true -> the pilot (DEVELOPER_CHAT_ID) stops
                                  getting these messages straight from the
                                  bot; set only after the tsekh-1 poller is
                                  verified, otherwise the pilot gets nothing.

Recording never raises: a journal problem must not break publishing. The
record_* calls return True only when the fact is queued with the author's
platform account; callers send directly otherwise, so the pilot never ends up
with neither a message nor an event. No Telegram id leaves the bot: keys and
payloads carry the platform account or the publication id only.
Decision record: DS-my-strategy/inbox/WP-568/DRR-f13-club-publisher-via-center.md
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional

from config import (
    CLUB_PUBLISHER_EVENTS_INSTANCE,
    CLUB_PUBLISHER_PILOT_VIA_CENTER,
    DEVELOPER_CHAT_ID,
    EVENT_GATEWAY_ENABLED,
    get_logger,
)

logger = get_logger(__name__)

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
_TITLE_MAX = 300  # club_publication.v1 maxLength


def classify_publish_failure(exc: BaseException) -> str:
    """Map a create_topic failure to the schema's failure class.

    DiscourseError carries "HTTP <code>" for answered requests and no code for
    network/timeout failures (clients/discourse.py `_request_with_retry`).
    A 4xx other than 429 means the club answered and refused the post; any
    other DiscourseError means the club could not be reached or failed itself.
    """
    from clients.discourse import DiscourseError

    if not isinstance(exc, DiscourseError):
        return "unknown"
    match = _HTTP_STATUS_RE.search(str(exc))
    if match:
        code = int(match.group(1))
        if 400 <= code < 500 and code != 429:
            return "club_rejected"
    return "club_unreachable"


def routes_via_center(chat_id: int) -> bool:
    """True when this author's publisher messages go through the center
    instead of a direct Telegram message from the bot."""
    return bool(
        CLUB_PUBLISHER_PILOT_VIA_CENTER
        and CLUB_PUBLISHER_EVENTS_INSTANCE
        and EVENT_GATEWAY_ENABLED
        and DEVELOPER_CHAT_ID
        and chat_id == DEVELOPER_CHAT_ID
    )


async def _resolve_account(chat_id: int) -> Optional[str]:
    from helpers.dual_write import resolve_ory_id_from_chat

    try:
        return await resolve_ory_id_from_chat(chat_id)
    except Exception as exc:
        logger.warning(f"[PublisherEvents] account lookup failed: {exc}")
        return None


async def _enqueue(external_id: str, event_type: str, account_id: Optional[str], payload: dict) -> bool:
    from db.connection import get_pool
    from db.queries.event_outbox import insert_outbox_row

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await insert_outbox_row(
                conn,
                external_id=external_id,
                event_type=event_type,
                account_id=account_id,
                payload=payload,
                occurred_at=datetime.now(timezone.utc),
            )
        return True
    except Exception as exc:
        logger.warning(f"[PublisherEvents] enqueue failed {external_id}: {exc}")
        return False


async def record_publication(
    chat_id: int,
    *,
    status: str,
    publication_id: int,
    title: Optional[str],
    scheduled_for: Optional[datetime] = None,
    topic_url: Optional[str] = None,
    failure: Optional[str] = None,
) -> bool:
    """Queue one club_publication.v1 fact. A naive `scheduled_for` is UTC (the
    publisher's slot convention). Returns True when queued with an account."""
    instance = CLUB_PUBLISHER_EVENTS_INSTANCE
    if not instance:
        return False
    payload: dict = {
        "status": status,
        "publication_id": publication_id,
        # minLength 1 in the schema: an empty title would be rejected forever.
        "title": (title or "(без названия)")[:_TITLE_MAX],
        "bot_instance": instance,
    }
    key = f"club-publication:{instance}:{publication_id}:{status}"
    if scheduled_for is not None:
        slot = scheduled_for.replace(tzinfo=timezone.utc) if scheduled_for.tzinfo is None else scheduled_for.astimezone(timezone.utc)
        payload["scheduled_for"] = slot.isoformat().replace("+00:00", "Z")
        key += f":{slot:%Y%m%dT%H%M}"
    if topic_url:
        payload["topic_url"] = topic_url
    if failure:
        payload["failure"] = failure
        # A post can fail again after being rescheduled: one fact per attempt.
        key += f":{datetime.now(timezone.utc):%Y%m%dT%H%M}"
    account_id = await _resolve_account(chat_id)
    queued = await _enqueue(key, "club_publication", account_id, payload)
    return queued and account_id is not None


async def record_queue_low(chat_id: int, *, pending_count: int, min_queue: int, day: date) -> bool:
    """Queue one club_publication_queue_low.v1 fact per author per Moscow day.
    The per-author key uses the platform account; without one there is no
    PII-free key, so nothing is queued."""
    instance = CLUB_PUBLISHER_EVENTS_INSTANCE
    if not instance:
        return False
    account_id = await _resolve_account(chat_id)
    if account_id is None:
        logger.info("[PublisherEvents] queue_low not recorded: author has no platform account")
        return False
    payload = {"pending_count": pending_count, "min_queue": min_queue, "bot_instance": instance}
    return await _enqueue(
        f"club-publication-queue-low:{instance}:{account_id}:{day.isoformat()}",
        "club_publication_queue_low",
        account_id,
        payload,
    )
