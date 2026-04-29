"""
Универсальная идемпотентность доставки сообщений (WP-152).

WP-268 Phase 5 cutover: idempotency gate перенесён с notification_log (bot_data)
на learning.domain_event (UNIQUE source+external_id). Паттерн Log-before-send
теперь пишет синхронно напрямую в learning BD через insert_domain_event_direct().

Заменяет 6+ разрозненных guard-ов:
- reminders.sent
- marathon_content.notification_sent_at
- nudge_log
- conversion_events (milestone dedup)
- trial expiry (без guard → теперь с guard)
- feed digest (без guard → теперь с guard)

Формат idempotency_key: {type}:{chat_id}:{date}:{detail}
"""

import json
import logging
import uuid as _uuid_mod
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

from db.connection import get_pool, get_learning_pool
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)


async def ensure_notification_log():
    """Создать таблицу notification_log (idempotent)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS notification_log (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                notification_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload JSONB DEFAULT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(idempotency_key)
            )
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_notification_log_chat_type
            ON notification_log(chat_id, notification_type)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_notification_log_created
            ON notification_log(created_at)
        ''')


async def insert_domain_event_direct(
    source: str,
    external_id: str,
    event_type: str,
    schema_version: str,
    occurred_at: datetime,
    account_id: Optional[str],
    payload: dict,
) -> bool:
    """Синхронный INSERT в learning.domain_event (WP-268 Phase 5 cutover).

    Idempotency gate: UNIQUE(source, external_id) в domain_event.
    Заменяет async event-gateway path для notification_sent events.

    Returns True если новая запись, False если дубль (ON CONFLICT DO NOTHING).
    """
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)

    account_uuid = None
    if account_id:
        try:
            account_uuid = _uuid_mod.UUID(account_id)
        except (ValueError, AttributeError):
            pass

    lp = await get_learning_pool()
    async with lp.acquire() as conn:
        row = await conn.fetchrow(
            '''INSERT INTO domain_event
               (source, external_id, event_type, schema_version,
                occurred_at, account_id, payload)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
               ON CONFLICT (source, external_id) DO NOTHING
               RETURNING id''',
            source, external_id, event_type, schema_version,
            occurred_at, account_uuid,
            json.dumps(payload),
        )
    return row is not None


async def try_insert_notification(
    chat_id: int,
    notification_type: str,
    idempotency_key: str,
    payload: Optional[dict] = None,
) -> bool:
    """Попытаться записать факт отправки уведомления.

    WP-268 Phase 5 cutover: пишет напрямую в learning.domain_event
    (синхронный INSERT через get_learning_pool()), не через notification_log.
    Idempotency gate: UNIQUE(source, external_id) в domain_event.
    external_id = f"notification-{idempotency_key}" — контракт с was_notification_sent().

    Returns:
        True если запись успешна (уведомление ещё не отправлялось).
        False если дубль (idempotency_key уже существует в domain_event).
    """
    ory = await resolve_ory_id_from_chat(chat_id)
    return await insert_domain_event_direct(
        source="aist-bot",
        external_id=f"notification-{idempotency_key}",
        event_type="notification_sent",
        schema_version="v1",
        occurred_at=datetime.utcnow(),
        account_id=ory,
        payload={
            "notification_type": notification_type,
            "idempotency_key": idempotency_key,
            "payload_keys": list(payload.keys()) if payload else [],
        },
    )


async def was_notification_sent(idempotency_key: str) -> bool:
    """Проверить, было ли уведомление уже отправлено.

    WP-269: чтение из learning.domain_event (notification_sent events).
    Idempotency через (source, external_id) UNIQUE: external_id = f"notification-{idempotency_key}".
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM domain_event "
            "WHERE source = 'aist-bot' AND event_type = 'notification_sent' AND external_id = $1",
            f"notification-{idempotency_key}",
        )
        return row is not None


async def send_idempotent(
    chat_id: int,
    notification_type: str,
    idempotency_key: str,
    send_fn: Callable[[], Awaitable],
    payload: Optional[dict] = None,
) -> bool:
    """Log-before-send с idempotency key.

    1. Записать в domain_event (log-before-send, §10.10, WP-268 cutover)
    2. Если запись успешна — вызвать send_fn()
    3. Если дубль — пропустить

    Returns:
        True если отправлено, False если дубль.
    """
    inserted = await try_insert_notification(
        chat_id, notification_type, idempotency_key, payload
    )
    if not inserted:
        logger.debug(
            f"[Notification] Skip duplicate: {notification_type} key={idempotency_key}"
        )
        return False

    await send_fn()
    logger.info(
        f"[Notification] Sent: {notification_type} chat={chat_id} key={idempotency_key}"
    )
    return True


async def was_nudge_sent_recently(chat_id: int, nudge_key: str, cooldown_days: int) -> bool:
    """Проверить cooldown nudge через learning.domain_event (WP-253 B-port).

    WP-253 B-port (28 апр): миграция с legacy `platform.notification_log` на
    `learning.public.domain_event` (event_type='notification_sent', source='aist-bot').

    Контракт writer'а (try_insert_notification:95): event.external_id = f"notification-{idempotency_key}".
    Idempotency key для nudge: f"nudge:{chat_id}:{date}:{nudge_key}".
    Поэтому LIKE-паттерн: 'notification-nudge:{chat_id}:%:{nudge_key}'.

    INVARIANT: writer ОБЯЗАН формировать external_id = f"notification-{idempotency_key}". Reader
    полагается на этот префикс. Если контракт сломается — reader даст тихо неверный результат.

    Ищет любую запись за последние cooldown_days дней. Index-friendly query через literal-prefix LIKE
    на UNIQUE indexed column external_id (verified via EXPLAIN: 0.027ms).
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT 1 FROM domain_event
               WHERE source = 'aist-bot'
                 AND event_type = 'notification_sent'
                 AND payload->>'notification_type' = 'nudge'
                 AND external_id LIKE $1
                 AND ingested_at >= NOW() - INTERVAL '1 day' * $2
               LIMIT 1""",
            f'notification-nudge:{chat_id}:%:{nudge_key}',
            cooldown_days,
        )
        return row is not None


async def get_notification_stats(chat_id: int, days: int = 30) -> dict:
    """Статистика уведомлений пользователя за N дней (WP-253 B-port).

    WP-253 B-port (28 апр): миграция на learning.public.domain_event.
    notification_type извлекается из payload jsonb (writer кладёт его в payload — см. try_insert_notification:101).

    Используется для Ф4 (notification_engagement → ЦД).
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT payload->>'notification_type' AS notification_type, COUNT(*) AS cnt
               FROM domain_event
               WHERE source = 'aist-bot'
                 AND event_type = 'notification_sent'
                 AND payload->>'idempotency_key' LIKE $1
                 AND ingested_at >= NOW() - INTERVAL '1 day' * $2
               GROUP BY 1""",
            f'%:{chat_id}:%',
            days,
        )
        return {row['notification_type']: row['cnt'] for row in rows if row['notification_type']}
