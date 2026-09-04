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
from datetime import datetime
from typing import Awaitable, Callable, Optional

from db.connection import get_pool
from db.queries.notifications import try_insert_notification

logger = logging.getLogger(__name__)


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

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Сериализация одновременных отправок одному пользователю в один день —
            # снимает гонку COUNT-then-INSERT на границе потолка (только этот scope, не весь журнал).
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"deliver:{chat_id}:{_today_utc().isoformat()}",
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
                         AND created_at::date = (NOW() AT TIME ZONE 'UTC')::date
                         AND status IN ('queued', 'sent')""",
                    chat_id, klass,
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

    deliver_fn(chat_id, content_spec) выполняет физическую отправку; бросает при отказе.

    Log-before-send (§10.10): помечаем sent + пишем журнал ДО отправки → дубля не будет
    при падении между отправкой и фиксацией. Цена — потеря при транзиентном отказе TG
    (тот же компромисс, что у reminders). Retry через транспорт DP.ROLE.044 — следующий шаг.
    """
    pool = await get_pool()
    delivered = 0
    failed = 0
    async with pool.acquire() as conn:
        # Явная транзакция: вне её asyncpg авто-коммитит SELECT и row-locks
        # отпускаются сразу — FOR UPDATE SKIP LOCKED переставал разводить
        # конкурентные consumers (redeploy-overlap: два инстанса берут одни строки).
        async with conn.transaction():
            rows = await conn.fetch(
                """SELECT id, chat_id, notification_class, payload, priority,
                          journal_key, journal_type
                   FROM development.notification_queue
                   WHERE status = 'queued' AND scheduled_at <= NOW()
                   ORDER BY priority ASC, scheduled_at ASC
                   LIMIT $1
                   FOR UPDATE SKIP LOCKED""",
                batch,
            )
            for row in rows:
                content_spec = row["payload"]
                if isinstance(content_spec, str):
                    content_spec = json.loads(content_spec)
                chat_id = row["chat_id"]
                klass = row["notification_class"]

                # log-before-send: журнал в learning.domain_event + статус sent.
                # Семантический ключ/тип от отправителя (Ф4) сохраняет контракт readers
                # (was_nudge_sent_recently: external_id LIKE 'notification-nudge:...',
                # get_notification_stats: payload->>'notification_type'). Fallback —
                # технический delivery:{id} (canary/ops-alert без семантики).
                inserted = await try_insert_notification(
                    chat_id=chat_id,
                    notification_type=row["journal_type"] or klass,
                    idempotency_key=row["journal_key"] or f"delivery:{row['id']}",
                    payload={"notification_class": klass, "priority": row["priority"]},
                    delivered_via="notification_service",
                )
                if not inserted:
                    # Журнал уже помнит ключ: отправлено старым кодом до деплоя или
                    # параллельным инстансом (redeploy-overlap). Дубль не доставляем.
                    await conn.execute(
                        """UPDATE development.notification_queue
                           SET status = 'suppressed', reason = 'journal-dup'
                           WHERE id = $1""",
                        row["id"],
                    )
                    logger.info(
                        "[Delivery] journal-dup skip id=%s chat=%s class=%s",
                        row["id"], chat_id, klass,
                    )
                    continue
                await conn.execute(
                    "UPDATE development.notification_queue SET status = 'sent', sent_at = NOW() WHERE id = $1",
                    row["id"],
                )
                try:
                    await deliver_fn(chat_id, content_spec)
                    try:
                        await conn.execute(
                            """UPDATE development.nudge_receipt
                               SET status = 'delivered', delivered_at = NOW()
                               WHERE queue_id = $1 AND status = 'reserved'""",
                            row["id"],
                        )
                    except Exception as receipt_error:
                        # Migration 043 is fail-open at boot for legacy deploys.
                        # Delivery must not fail after transport acknowledgement;
                        # a missing settlement remains safely at-most-once.
                        logger.warning(
                            "[Delivery] receipt settlement failed id=%s: %s",
                            row["id"], receipt_error,
                        )
                    delivered += 1
                except Exception as e:
                    failed += 1
                    logger.error(
                        "[Delivery] deliver failed id=%s chat=%s class=%s: %s",
                        row["id"], chat_id, klass, e,
                    )

    if delivered or failed:
        logger.info("[Delivery] drain: delivered=%s failed=%s", delivered, failed)
    return {"delivered": delivered, "failed": failed}


# ─────────────────────────────────────────────────────────────────────────────
# Внутренние помощники.
# ─────────────────────────────────────────────────────────────────────────────

def _today_utc() -> datetime.date:
    # Naive UTC date — согласован с created_at::date в COUNT (NOW() AT TIME ZONE 'UTC').
    return datetime.utcnow().date()


async def _is_opted_out(conn, chat_id: int, klass: str) -> bool:
    """Hard-gate предпочтений. Единая точка проверки opt-out.

    Хранилища предпочтений per-user пока нет (DP.SC.177 «Чего НЕТ»). Это чокпоинт:
    когда появится store (категории владеет DP.SC.116) — подключается здесь, остальной
    код не меняется. Сейчас честно возвращает False (никто не отписан).
    """
    return False


async def _is_duplicate(conn, dedup_key: str, window_hours: int) -> bool:
    row = await conn.fetchrow(
        """SELECT 1 FROM development.notification_queue
           WHERE dedup_key = $1 AND status IN ('queued', 'sent')
             AND created_at >= NOW() - INTERVAL '1 hour' * $2
           LIMIT 1""",
        dedup_key, window_hours,
    )
    return row is not None


async def _record_suppressed(
    conn, chat_id: int, klass: str, content_spec: dict,
    priority: int, dedup_key: Optional[str], reason: str,
) -> dict:
    """Записать подавление в очередь (аудит, не молчать) + лог."""
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
