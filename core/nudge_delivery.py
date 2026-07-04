from __future__ import annotations

"""
WP-117 Ф-decouple deliverables (contract: DS-my-strategy/inbox/WP-117/f-decouple-contract.md).

Boundary (contract §1): WP-117 (producer) owns rule semantics — which candidates
to generate, when, personalized how. This module (WP-418, policy) owns whether
and how to deliver — cooldown, class cap, opt-out, channel.

Deliberately does NOT call notification_service.enqueue() or reuse CLASS_POLICY:
that engine's policy is keyed by the 5-class model (critical/must-deliver/...),
not by nudge_type. Routing a nudge_type through enqueue(klass=...) would apply
whichever CLASS_POLICY entry matches the class label — a different cooldown/cap
than NUDGE_TYPE_CONFIG says for that specific type — producing a silent policy
conflict rather than a single source of truth (peer-session 2026-07-04-11).

Shares only the queue table with notification_service; CLASS_CAPPED nudge types
are inserted with notification_class='capped' so they count against the SAME
daily-2 bucket as the 65 legacy senders (DP.SC.177: the cap is per-place/
source-agnostic, not per-sender). Journaling to the domain_event idempotency
log happens at drain() time (shared consumer), not here.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from db.connection import get_pool
from db.queries.notifications import fetch_recent_nudges_by_type

logger = logging.getLogger(__name__)


class ClassCap(Enum):
    CLASS_CAPPED = "capped"        # делит потолок ≤2/день с legacy capped-отправителями
    CLASS_ANY = "any"              # независимый, без общего потолка
    CLASS_EXCLUSIVE = "exclusive"  # единственный в батче для пользователя


@dataclass(frozen=True)
class NudgeTypeConfig:
    nudge_type: str
    cooldown_days: int  # 0 = кулдаун не применяется (окно схлопывается в NOW())
    class_cap: ClassCap
    channel_defaults: list[str] = field(default_factory=lambda: ["telegram"])


@dataclass(frozen=True)
class NudgeCandidate:
    """Форма, которую отдаёт producer WP-117 (contract §2.1). Определена здесь,
    т.к. producer ещё не реализован — WP-418 фиксирует ожидаемый вход."""
    user_id: int
    nudge_type: str
    payload: dict
    dedup_key: str
    priority: int


@dataclass(frozen=True)
class NudgeRecord:
    user_id: int
    nudge_type: str
    sent_at: datetime
    status: str


@dataclass(frozen=True)
class EnqueueResult:
    user_id: int
    nudge_type: str
    enqueued: bool
    reason: Optional[str] = None


# Наполняется по мере переноса правил WP-117 (contract §5). Пусто = ни один тип
# ещё не подключён; select_and_enqueue честно отклоняет с reason=unknown_type.
NUDGE_TYPE_CONFIG: dict[str, NudgeTypeConfig] = {}


async def get_recent_nudges_batch(
    user_ids: list[int],
    nudge_types: Optional[list[str]] = None,
) -> dict[int, list[NudgeRecord]]:
    """Contract §2.2. Lookback per-type берётся из NUDGE_TYPE_CONFIG.cooldown_days."""
    types = nudge_types if nudge_types is not None else list(NUDGE_TYPE_CONFIG)
    cooldowns = {t: NUDGE_TYPE_CONFIG[t].cooldown_days for t in types if t in NUDGE_TYPE_CONFIG}
    raw = await fetch_recent_nudges_by_type(user_ids, cooldowns)
    return {
        uid: [
            NudgeRecord(user_id=uid, nudge_type=r["nudge_type"], sent_at=r["sent_at"], status=r["status"])
            for r in records
        ]
        for uid, records in raw.items()
    }


async def select_and_enqueue(candidates: list[NudgeCandidate]) -> list[EnqueueResult]:
    """Contract §2.2: группирует по user_id, применяет cooldown/class_cap/opt-out,
    пишет в очередь доставки, возвращает результат по каждому кандидату."""
    if not candidates:
        return []

    by_user: dict[int, list[NudgeCandidate]] = {}
    for c in candidates:
        by_user.setdefault(c.user_id, []).append(c)

    results: list[EnqueueResult] = []
    for user_id, user_candidates in by_user.items():
        results.extend(await _select_and_enqueue_for_user(user_id, user_candidates))
    return results


async def _select_and_enqueue_for_user(
    user_id: int, candidates: list[NudgeCandidate]
) -> list[EnqueueResult]:
    unknown = [c for c in candidates if c.nudge_type not in NUDGE_TYPE_CONFIG]
    known = [c for c in candidates if c.nudge_type in NUDGE_TYPE_CONFIG]

    results = [
        EnqueueResult(user_id=user_id, nudge_type=c.nudge_type, enqueued=False, reason="unknown_type")
        for c in unknown
    ]
    if not known:
        return results

    if await _is_opted_out(user_id):
        results.extend(
            EnqueueResult(user_id=user_id, nudge_type=c.nudge_type, enqueued=False, reason="opt-out")
            for c in known
        )
        return results

    # CLASS_EXCLUSIVE preempts: если хоть один exclusive-кандидат проходит, он
    # один уходит в очередь, остальные (любого типа) подавляются этим батчем.
    exclusive = [c for c in known if NUDGE_TYPE_CONFIG[c.nudge_type].class_cap == ClassCap.CLASS_EXCLUSIVE]
    if exclusive:
        chosen = min(exclusive, key=lambda c: c.priority)
        preempted = [c for c in known if c is not chosen]
        results.append(await _try_enqueue_one(user_id, chosen))
        results.extend(
            EnqueueResult(user_id=user_id, nudge_type=c.nudge_type, enqueued=False, reason="exclusive-preempted")
            for c in preempted
        )
        return results

    for candidate in known:
        results.append(await _try_enqueue_one(user_id, candidate))
    return results


async def _try_enqueue_one(user_id: int, candidate: NudgeCandidate) -> EnqueueResult:
    config = NUDGE_TYPE_CONFIG[candidate.nudge_type]

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # ТОТ ЖЕ буквальный ключ, что notification_service.enqueue()
            # (core/notification_service.py:126) — сериализует оба движка друг
            # против друга на общем бакете 'capped' одного чата за день. Разные
            # строки здесь = разные локи → COUNT-then-INSERT race между
            # движками остаётся открытым (найдено code review 2026-07-04-11).
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"deliver:{user_id}:{_today_utc().isoformat()}",
            )

            duplicate = await conn.fetchrow(
                """SELECT 1 FROM development.notification_queue
                   WHERE dedup_key = $1 AND status IN ('queued', 'sent')
                     AND created_at >= NOW() - INTERVAL '1 day' * $2
                   LIMIT 1""",
                candidate.dedup_key, config.cooldown_days,
            )
            if duplicate:
                return EnqueueResult(
                    user_id=user_id, nudge_type=candidate.nudge_type,
                    enqueued=False, reason="cooldown",
                )

            if config.class_cap == ClassCap.CLASS_CAPPED:
                used = await conn.fetchval(
                    """SELECT count(*) FROM development.notification_queue
                       WHERE chat_id = $1 AND notification_class = 'capped'
                         AND created_at::date = (NOW() AT TIME ZONE 'UTC')::date
                         AND status IN ('queued', 'sent')""",
                    user_id,
                )
                if used >= 2:
                    return EnqueueResult(
                        user_id=user_id, nudge_type=candidate.nudge_type,
                        enqueued=False, reason="cap-exceeded",
                    )

            queue_class = "capped" if config.class_cap == ClassCap.CLASS_CAPPED else candidate.nudge_type
            idempotency_key = f"nudge:{user_id}:{_today_utc().isoformat()}:{candidate.nudge_type}"
            await conn.fetchrow(
                """INSERT INTO development.notification_queue
                   (chat_id, notification_class, payload, priority, dedup_key,
                    journal_key, journal_type, status)
                   VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, 'queued')
                   RETURNING id""",
                user_id, queue_class, json.dumps(candidate.payload), candidate.priority,
                candidate.dedup_key, idempotency_key, "nudge",
            )

    return EnqueueResult(user_id=user_id, nudge_type=candidate.nudge_type, enqueued=True, reason=None)


async def _is_opted_out(user_id: int) -> bool:
    """Hard-gate предпочтений (contract §2.2). Хранилища per-user пока нет (тот же
    чокпоинт, что notification_service._is_opted_out) — честно возвращает False."""
    return False


def _today_utc() -> datetime.date:
    return datetime.utcnow().date()
