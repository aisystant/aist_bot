from __future__ import annotations

"""
Доставщик — слой политики доставки исходящих сообщений (WP-418 Ф3).

# see DP.SC.177, DP.ROLE.075

Единая воронка всех исходящих пользователю. Применяет:
  предпочтения (hard-gate) → дедуп → потолок класса → приоритетная очередь → дренаж.

Транспорт (физическая отправка в Telegram) — НЕ здесь: drain() принимает deliver_fn,
которую даёт планировщик (Bot.send_message). Это держит модуль чистым и тестируемым
без aiogram (различение «суть ≠ интерфейс»).

Архитектура согласована на ArchGate WP-418 Ф1 (peer-сессия 2026-06-11-41):
  - модуль в боте, очередь приватна (снаружи raw SQL запрещён);
  - COUNT по собственной очереди + advisory-lock на (chat, day) против гонки на границе потолка;
  - схема очереди pgqueuer-ready (priority, dedup_key, scheduled_at, locked_at, attempts).

Очередь живёт в основной базе бота (get_pool), журнал отправок — в learning.domain_event
(через db.queries.notifications, переиспользуется как идемпотентная шина).
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from db.connection import get_pool
from db.queries.notifications import try_insert_notification, was_notification_sent

logger = logging.getLogger(__name__)

# Day boundary for caps and the per-user lock: Moscow, not UTC (pilot audience).
DELIVERY_TZ = "Europe/Moscow"

# Delivery attempts per row before a transient failure becomes permanent.
MAX_ATTEMPTS = 3
# A row left in 'sending' longer than this (process died mid-call) is settled as
# 'unknown': the request may or may not have reached Telegram, so no retry.
STALE_SENDING_MINUTES = 10


class DeliveryBlocked(Exception):
    """Transport: the user blocked the bot or the chat is gone. No retry."""


class DeliveryPermanent(Exception):
    """Transport: Telegram rejected the message for good (bad request). No retry."""


class DeliveryTransient(Exception):
    """Transport: rate limit or server error before acceptance. Retry later."""

# Any other exception from deliver_fn means the outcome is unknown (timeout,
# network error after the request left): the row is settled as 'unknown'
# without retry, because a duplicate is worse than a loss for nudges.


# ─────────────────────────────────────────────────────────────────────────────
# Класс-модель (WP-418 Ф2, DP.SC.177). Пять классов с политикой на каждый.
# ─────────────────────────────────────────────────────────────────────────────

CLASS_CRITICAL = "critical"            # безопасность/деньги — обходит всё
CLASS_MUST_DELIVER = "must-deliver"    # подписочный урок, чек-ин — вне дневного потолка
CLASS_TRANSACTIONAL = "transactional"  # /remind, OAuth «X подключён»
CLASS_CAPPED = "capped"                # реанимация, engagement/practice nudge
CLASS_OPS_ALERT = "ops-alert"          # autofix/health/dev — служебный канал


@dataclass(frozen=True)
class ClassPolicy:
    daily_cap: Optional[int]   # None = без дневного лимита
    priority: int              # 1 = высший
    dedup_hours: int           # окно дедупа по dedup_key
    channel: str               # user | dev
    opt_out: bool              # применять ли hard-gate предпочтений


# The delivery day of a queue row: the daily cap (here and in nudge_delivery)
# and the suppression audit (_record_suppressed) must share one boundary, or a
# repeat would resolve to a row the cap no longer counts. The queries stay
# static strings (no SQL assembled at runtime); a smoke test checks that every
# one of them carries this boundary, with $N standing for DELIVERY_TZ.
DELIVERY_DAY_BOUNDARY = (
    "(created_at AT TIME ZONE 'UTC' AT TIME ZONE $N)::date = (NOW() AT TIME ZONE $N)::date"
)

# Queue rows of user reminders (WP-568 F16 step 1b): the drain writes the
# reminder_delivered event only after Telegram accepted the message.
REMINDER_JOURNAL_TYPE = "reminder"


# daily_cap у capped = 2/день СУММАРНО по классу (гарантия, которую один отправитель дать не может).
CLASS_POLICY: dict[str, ClassPolicy] = {
    CLASS_CRITICAL:      ClassPolicy(daily_cap=None, priority=1, dedup_hours=24, channel="user", opt_out=False),
    CLASS_MUST_DELIVER:  ClassPolicy(daily_cap=None, priority=2, dedup_hours=12, channel="user", opt_out=False),
    CLASS_TRANSACTIONAL: ClassPolicy(daily_cap=None, priority=3, dedup_hours=6,  channel="user", opt_out=False),
    CLASS_CAPPED:        ClassPolicy(daily_cap=2,    priority=4, dedup_hours=48, channel="user", opt_out=True),
    CLASS_OPS_ALERT:     ClassPolicy(daily_cap=None, priority=9, dedup_hours=1,  channel="dev",  opt_out=False),
}


# ─────────────────────────────────────────────────────────────────────────────
# Приём (enqueue) — единая точка входа для in-bot отправителей.
# ─────────────────────────────────────────────────────────────────────────────

async def enqueue(
    chat_id: int,
    klass: str,
    content_spec: dict,
    priority: Optional[int] = None,
    dedup_key: Optional[str] = None,
    journal_key: Optional[str] = None,
    journal_type: Optional[str] = None,
) -> dict:
    """Принять сообщение в воронку доставки.

    content_spec — канало-нейтральный: {"text": str, ...}. БЕЗ chat_id/reply_markup
    (рендер канала — в транспорте/дренаже). Это инвариант DP.SC.177.
    Поле "format" ("markdown"|"html"|"plain") — прагматичный компромисс одного
    канала (Ф4, peer-сессия 2026-06-12-06); при Ф5 (мультиканальность) пересмотр
    на структурированные блоки.

    journal_key/journal_type — семантический журнал доставки (Ф4): drain пишет
    learning.domain_event с external_id = f"notification-{journal_key}" и
    notification_type = journal_type. Контракт readers (was_nudge_sent_recently,
    get_notification_stats) сохраняется при миграции отправителей: передавать
    ПРЕЖНИЙ idempotency_key и ПРЕЖНИЙ notification_type ('nudge', 'marathon_nudge',
    ...), не класс. Fallback (None) = f"delivery:{queue_id}" / klass — только для
    canary/ops-alert.

    Returns: {"notification_id": int|None, "status": "queued"|"suppressed", "reason"?: str}
    Suppression reasons: already-sent (journal_key in the delivery journal),
    opt-out, blocked, duplicate, cap-exceeded.
    """
    policy = CLASS_POLICY.get(klass)
    if policy is None:
        raise ValueError(f"unknown notification class: {klass!r}")
    text = (content_spec or {}).get("text")
    if not text:
        raise ValueError("content_spec.text is required")
    # Валидация actions на входе: KeyError в drain случился бы ПОСЛЕ status=sent —
    # сообщение терялось бы молча. Громкая ошибка на стороне отправителя дешевле.
    for action in (content_spec or {}).get("actions") or []:
        if not action.get("label"):
            raise ValueError("content_spec.actions: label is required")
        cb = action.get("action")
        url = action.get("url")
        if not cb and not url:
            raise ValueError("content_spec.actions: each action requires 'action' (callback_data) or 'url'")
        if cb and len(str(cb).encode()) > 64:
            raise ValueError(
                f"content_spec.actions: callback_data >64 bytes (Telegram limit): {cb!r}"
            )
    if priority is None:
        priority = policy.priority

    # A key already in the delivery journal (sent by this queue earlier than the
    # dedup window, or by the pre-queue code) is a repeat: refuse it here, before
    # a row exists. Otherwise a sender polling every 10 minutes adds a row per
    # tick that the drain then suppresses as journal-dup (WP-568 F16, 12.11).
    if journal_key:
        try:
            already_sent = await was_notification_sent(journal_key)
        except Exception as e:
            # Journal unreachable: let the row in; the drain checks the journal again.
            logger.warning("[Delivery] journal check failed key=%s: %s", journal_key, e)
            already_sent = False
        if already_sent:
            logger.info(
                "[Delivery] journal skip chat=%s class=%s key=%s", chat_id, klass, journal_key
            )
            return {"notification_id": None, "status": "suppressed", "reason": "already-sent"}

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Сериализация одновременных отправок одному пользователю в один день —
            # снимает гонку COUNT-then-INSERT на границе потолка (только этот scope, не весь журнал).
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                delivery_lock_key(chat_id),
            )

            # Optional nudges to users who blocked the bot never enter the queue.
            # Other classes still try: bot_blocked clears only when the user
            # writes, and a critical message (payment, access) to someone who
            # already unblocked must not be lost; the transport marks real blocks.
            if klass == CLASS_CAPPED and await _is_bot_blocked(conn, chat_id):
                return await _record_suppressed(
                    conn, chat_id, klass, content_spec, priority, dedup_key, "blocked"
                )

            if policy.opt_out and await _is_opted_out(conn, chat_id, klass):
                return await _record_suppressed(
                    conn, chat_id, klass, content_spec, priority, dedup_key, "opt-out"
                )

            if dedup_key and await _is_duplicate(conn, dedup_key, policy.dedup_hours):
                logger.info(
                    "[Delivery] dedup skip chat=%s class=%s key=%s", chat_id, klass, dedup_key
                )
                return {"notification_id": None, "status": "suppressed", "reason": "duplicate"}

            if policy.daily_cap is not None:
                used = await conn.fetchval(
                    """SELECT count(*) FROM development.notification_queue
                       WHERE chat_id = $1 AND notification_class = $2
                         AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE $3)::date = (NOW() AT TIME ZONE $3)::date
                         AND status IN ('queued', 'sending', 'sent', 'unknown')""",
                    chat_id, klass, DELIVERY_TZ,
                )
                if used >= policy.daily_cap:
                    return await _record_suppressed(
                        conn, chat_id, klass, content_spec, priority, dedup_key, "cap-exceeded"
                    )

            row = await conn.fetchrow(
                """INSERT INTO development.notification_queue
                   (chat_id, notification_class, payload, priority, dedup_key,
                    journal_key, journal_type, status)
                   VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, 'queued')
                   RETURNING id""",
                chat_id, klass, json.dumps(content_spec), priority, dedup_key,
                journal_key, journal_type,
            )
            return {"notification_id": row["id"], "status": "queued"}


# ─────────────────────────────────────────────────────────────────────────────
# Дренаж — единственный consumer очереди. Сортирует по приоритету, отдаёт транспорту.
# ─────────────────────────────────────────────────────────────────────────────

async def drain(
    deliver_fn: Callable[[int, dict], Awaitable[None]],
    batch: int = 20,
) -> dict:
    """Разобрать очередь: взять queued по приоритету, доставить через deliver_fn.

    deliver_fn(chat_id, content_spec) выполняет физическую отправку. Исход —
    по тому, как она завершилась (РП-568 Ф16, шаг 1а):
      вернула             → sent (журнал domain_event пишется только здесь);
      DeliveryBlocked     → blocked, без повтора;
      DeliveryPermanent   → failed_permanent, без повтора;
      DeliveryTransient   → снова queued с отсрочкой 2^attempts мин, не больше
                            MAX_ATTEMPTS попыток, затем failed_permanent;
      любое другое        → unknown: запрос мог дойти, повтор дал бы дубль.

    Строки забираются короткой транзакцией и переводятся в 'sending' ДО сетевого
    вызова; сам вызов идёт вне транзакции (раньше пакет из 20 строк держал одну
    транзакцию на все сетевые вызовы, и сбой посреди пакета терял остаток).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _settle_stale_sending(conn)
        rows = await _claim_batch(conn, batch)

        counts = {"delivered": 0, "failed": 0}
        attempted: set[int] = set()
        try:
            for row in rows:
                attempted.add(row["id"])
                outcome = await _deliver_row(conn, row, deliver_fn)
                if outcome == "sent":
                    counts["delivered"] += 1
                elif outcome != "suppressed":
                    counts["failed"] += 1
        finally:
            # A failure mid-batch must not strand rows that were never handed to
            # Telegram: they go back to the queue instead of ageing into 'unknown'.
            untouched = [row["id"] for row in rows if row["id"] not in attempted]
            if untouched:
                await _release_claim(conn, untouched)

    if counts["delivered"] or counts["failed"]:
        logger.info("[Delivery] drain: delivered=%s failed=%s",
                    counts["delivered"], counts["failed"])
    return counts


async def _claim_batch(conn, batch: int) -> list:
    """Забрать пакет строк и пометить их 'sending' одной короткой транзакцией.

    FOR UPDATE SKIP LOCKED разводит два экземпляра бота при выкатке: строку,
    взятую одним, второй не видит, а после коммита она уже не 'queued'.
    """
    async with conn.transaction():
        rows = await conn.fetch(
            """SELECT id, chat_id, notification_class, payload, priority,
                      journal_key, journal_type, attempts
               FROM development.notification_queue
               WHERE status = 'queued' AND scheduled_at <= NOW()
               ORDER BY priority ASC, scheduled_at ASC
               LIMIT $1
               FOR UPDATE SKIP LOCKED""",
            batch,
        )
        if rows:
            await conn.execute(
                """UPDATE development.notification_queue
                   SET status = 'sending', locked_at = NOW(),
                       attempts = COALESCE(attempts, 0) + 1
                   WHERE id = ANY($1::int[])""",
                [row["id"] for row in rows],
            )
    return list(rows)


async def _deliver_row(conn, row, deliver_fn) -> str:
    """Доставить одну забранную строку и записать её исход. Возвращает исход.

    A database error before the transport call puts the row back to 'queued'
    (nothing was sent); one after the transport call is logged and the row is
    left in 'sending' (the stale sweep settles it as 'unknown', never re-sends).
    """
    try:
        return await _deliver_row_unguarded(conn, row, deliver_fn)
    except _RowAfterSendError as e:
        logger.error("[Delivery] bookkeeping failed after the transport call id=%s: %s",
                     row["id"], e.__cause__)
        return "unknown"
    except Exception as e:
        logger.error("[Delivery] row failed before delivery id=%s: %s", row["id"], e)
        await _release_claim(conn, [row["id"]])
        return "failed_transient"


class _RowAfterSendError(Exception):
    """Bookkeeping failed after Telegram already accepted the message."""


async def _release_claim(conn, queue_ids: list[int]) -> None:
    try:
        await conn.execute(
            """UPDATE development.notification_queue
               SET status = 'queued', locked_at = NULL
               WHERE id = ANY($1::int[]) AND status = 'sending'""",
            queue_ids,
        )
    except Exception as e:
        # The stale sweep will settle these rows as 'unknown' in 10 minutes.
        logger.error("[Delivery] could not release claimed rows %s: %s", queue_ids, e)


async def _deliver_row_unguarded(conn, row, deliver_fn) -> str:
    content_spec = row["payload"]
    if isinstance(content_spec, str):
        content_spec = json.loads(content_spec)
    queue_id = row["id"]
    chat_id = row["chat_id"]
    klass = row["notification_class"]
    journal_key = row["journal_key"] or f"delivery:{queue_id}"

    # Журнал помнит ключ: отправлено старым кодом до деплоя или параллельным
    # экземпляром. Дубль не доставляем.
    try:
        already_sent = await was_notification_sent(journal_key)
    except Exception as e:
        # Journal unreachable: nothing was sent yet, so this is safely retryable.
        return await _retry_or_give_up(conn, row, e)
    if already_sent:
        await _set_status(conn, queue_id, "suppressed", "journal-dup")
        logger.info("[Delivery] journal-dup skip id=%s chat=%s class=%s",
                    queue_id, chat_id, klass)
        return "suppressed"

    try:
        try:
            await deliver_fn(chat_id, content_spec)
        except DeliveryBlocked as e:
            await _set_status(conn, queue_id, "blocked", str(e)[:200])
            return "blocked"
        except DeliveryPermanent as e:
            await _set_status(conn, queue_id, "failed_permanent", str(e)[:200])
            logger.warning("[Delivery] permanent failure id=%s chat=%s class=%s: %s",
                           queue_id, chat_id, klass, e)
            return "failed_permanent"
        except DeliveryTransient as e:
            return await _retry_or_give_up(conn, row, e)
        except Exception as e:
            await _set_status(conn, queue_id, "unknown", f"{type(e).__name__}: {e}"[:200])
            logger.error("[Delivery] outcome unknown id=%s chat=%s class=%s: %s",
                         queue_id, chat_id, klass, e)
            return "unknown"
        await conn.execute(
            """UPDATE development.notification_queue
               SET status = 'sent', sent_at = NOW() WHERE id = $1""",
            queue_id,
        )
    except Exception as e:
        # The transport was already called: the row must never return to the
        # queue from here, or a message Telegram accepted could go out twice.
        raise _RowAfterSendError() from e
    await _journal_sent(row, klass, journal_key)
    await _settle_receipt(conn, queue_id)
    if row["journal_type"] == REMINDER_JOURNAL_TYPE:
        await _log_reminder_delivered(chat_id, content_spec)
    return "sent"


async def _log_reminder_delivered(chat_id: int, content_spec: dict) -> None:
    """Keep the reminder_delivered event meaning «reached Telegram».

    Its readers (activity stats) count deliveries; the sender only knows that
    the reminder was queued. The event fields travel in content_spec["meta"].
    A failure here does not undo the delivery: the row is already 'sent'.
    """
    from db.queries.events import log_event

    try:
        await log_event(chat_id, "reminder_delivered", content_spec.get("meta") or {})
    except Exception as e:
        logger.error("[Delivery] reminder_delivered event failed chat=%s: %s", chat_id, e)


async def _retry_or_give_up(conn, row, error: Exception) -> str:
    attempts = row["attempts"] + 1 if row["attempts"] is not None else 1
    if attempts >= MAX_ATTEMPTS:
        await _set_status(conn, row["id"], "failed_permanent",
                          f"retries-exhausted: {error}"[:200])
        logger.warning("[Delivery] retries exhausted id=%s chat=%s: %s",
                       row["id"], row["chat_id"], error)
        return "failed_permanent"
    await conn.execute(
        """UPDATE development.notification_queue
           SET status = 'queued', locked_at = NULL, reason = $2,
               scheduled_at = NOW() + INTERVAL '1 minute' * $3
           WHERE id = $1""",
        row["id"], f"transient: {error}"[:200], 2 ** attempts,
    )
    logger.info("[Delivery] transient failure id=%s attempt=%s, retry scheduled: %s",
                row["id"], attempts, error)
    return "failed_transient"


async def _journal_sent(row, klass: str, journal_key: str) -> None:
    """Записать факт доставки в журнал с прежним ключом и типом отправителя.

    Контракт читателей (was_nudge_sent_recently, отбор напоминаний о практике,
    сверка конвейеров) держится на прежних idempotency_key и notification_type.
    Сбой журнала после доставки не отменяет её: строка очереди уже 'sent'.
    """
    try:
        await try_insert_notification(
            chat_id=row["chat_id"],
            notification_type=row["journal_type"] or klass,
            idempotency_key=journal_key,
            payload={"notification_class": klass, "priority": row["priority"]},
            delivered_via="notification_service",
        )
    except Exception as e:
        logger.error("[Delivery] journal write failed after delivery id=%s: %s",
                     row["id"], e)


async def _settle_receipt(conn, queue_id: int) -> None:
    try:
        await conn.execute(
            """UPDATE development.nudge_receipt
               SET status = 'delivered', delivered_at = NOW()
               WHERE queue_id = $1 AND status = 'reserved'""",
            queue_id,
        )
    except Exception as receipt_error:
        # Migration 043 is fail-open at boot for legacy deploys; a missing
        # settlement remains safely at-most-once.
        logger.warning("[Delivery] receipt settlement failed id=%s: %s",
                       queue_id, receipt_error)


async def _settle_stale_sending(conn) -> None:
    """Строки, застрявшие в 'sending' (процесс упал посреди вызова), → 'unknown'."""
    await conn.execute(
        """UPDATE development.notification_queue
           SET status = 'unknown', reason = 'stale-sending'
           WHERE status = 'sending'
             AND locked_at < NOW() - INTERVAL '1 minute' * $1""",
        STALE_SENDING_MINUTES,
    )


async def _set_status(conn, queue_id: int, status: str, reason: str) -> None:
    await conn.execute(
        """UPDATE development.notification_queue
           SET status = $2, reason = $3 WHERE id = $1""",
        queue_id, status, reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Внутренние помощники.
# ─────────────────────────────────────────────────────────────────────────────

def delivery_lock_key(chat_id: int) -> str:
    """Per-user, per-day advisory lock key shared by every queue writer.

    Both enqueue paths (this module and core.nudge_delivery) must serialise on
    the same key, or the cap COUNT-then-INSERT race reopens between them.
    """
    return f"deliver:{chat_id}:{_today_local().isoformat()}"


def _today_local() -> date:
    # Moscow date — the same boundary the cap COUNT uses (DELIVERY_TZ).
    return datetime.now(ZoneInfo(DELIVERY_TZ)).date()


async def _is_bot_blocked(conn, chat_id: int) -> bool:
    return bool(await conn.fetchval(
        "SELECT bot_blocked FROM development.user_state WHERE chat_id = $1",
        chat_id,
    ))


async def _is_opted_out(conn, chat_id: int, klass: str) -> bool:
    """Hard-gate предпочтений. Единая точка проверки opt-out.

    Хранилища предпочтений per-user пока нет (DP.SC.177 «Чего НЕТ»). Это чокпоинт:
    когда появится store (категории владеет DP.SC.116) — подключается здесь, остальной
    код не меняется. Сейчас честно возвращает False (никто не отписан).
    """
    return False


async def was_queue_row_sent(dedup_key: str) -> bool:
    """True if a queue row with this key was ever delivered (no time window).

    For senders whose key is unique per message (a reminder id or a date): the
    journal may miss a delivery when its write failed after the send, and the
    dedup window of the class is too short to cover a retry hours later.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT 1 FROM development.notification_queue
               WHERE dedup_key = $1 AND status = 'sent' LIMIT 1""",
            dedup_key,
        )
    return row is not None


async def _is_duplicate(conn, dedup_key: str, window_hours: int) -> bool:
    row = await conn.fetchrow(
        """SELECT 1 FROM development.notification_queue
           WHERE dedup_key = $1 AND status IN ('queued', 'sending', 'sent', 'unknown')
             AND created_at >= NOW() - INTERVAL '1 hour' * $2
           LIMIT 1""",
        dedup_key, window_hours,
    )
    return row is not None


async def _record_suppressed(
    conn, chat_id: int, klass: str, content_spec: dict,
    priority: int, dedup_key: Optional[str], reason: str,
) -> dict:
    """Записать подавление в очередь (аудит, не молчать) + лог.

    A sender that polls (practice nudges every 10 minutes) hits the same
    suppression on every tick. One audit row per (chat, class, key, reason) and
    delivery day is enough: a repeat returns that row instead of adding another
    (WP-568 F16 step 1b, second wave). The day boundary is the one the daily cap
    uses, so a repeat never resolves to yesterday's row. The caller holds the
    per-chat advisory lock, so two ticks cannot both miss the existing row.
    """
    if dedup_key:
        existing = await conn.fetchval(
            """SELECT id FROM development.notification_queue
               WHERE chat_id = $1 AND notification_class = $2 AND dedup_key = $3
                 AND status = 'suppressed' AND reason = $4
                 AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE $5)::date = (NOW() AT TIME ZONE $5)::date
               ORDER BY id LIMIT 1""",
            chat_id, klass, dedup_key, reason, DELIVERY_TZ,
        )
        if existing is not None:
            logger.debug(
                "[Delivery] suppressed again chat=%s class=%s reason=%s (row %s)",
                chat_id, klass, reason, existing,
            )
            return {"notification_id": existing, "status": "suppressed",
                    "reason": reason, "deduped": True}
    row = await conn.fetchrow(
        """INSERT INTO development.notification_queue
           (chat_id, notification_class, payload, priority, dedup_key, status, reason)
           VALUES ($1, $2, $3::jsonb, $4, $5, 'suppressed', $6)
           RETURNING id""",
        chat_id, klass, json.dumps(content_spec), priority, dedup_key, reason,
    )
    logger.info(
        "[Delivery] suppressed chat=%s class=%s reason=%s", chat_id, klass, reason
    )
    return {"notification_id": row["id"], "status": "suppressed", "reason": reason}
