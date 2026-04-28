"""
Универсальная идемпотентность доставки сообщений (WP-152).

Единая таблица notification_log с idempotency key заменяет 6+ разрозненных guard-ов:
- reminders.sent
- marathon_content.notification_sent_at
- nudge_log
- conversion_events (milestone dedup)
- trial expiry (без guard → теперь с guard)
- feed digest (без guard → теперь с guard)

Паттерн: Log-before-send (§10.10). Запись в notification_log ПЕРЕД отправкой.
Формат idempotency_key: {type}:{chat_id}:{date}:{detail}
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, Callable, Awaitable

from db.connection import get_pool, get_learning_pool
from helpers.dual_write import post_event, resolve_ory_id_from_chat

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


async def try_insert_notification(
    chat_id: int,
    notification_type: str,
    idempotency_key: str,
    payload: Optional[dict] = None,
) -> bool:
    """Попытаться записать факт отправки уведомления.

    Returns:
        True если запись успешна (уведомление ещё не отправлялось).
        False если idempotency_key уже существует (дубль).
    """
    pool = await get_pool()
    inserted = False
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                '''INSERT INTO notification_log
                   (chat_id, notification_type, idempotency_key, payload)
                   VALUES ($1, $2, $3, $4::jsonb)''',
                chat_id, notification_type, idempotency_key,
                json.dumps(payload) if payload else None,
            )
            inserted = True
        except Exception as e:
            # UniqueViolationError → уже отправлено (дубль)
            if 'unique' in str(e).lower() or '23505' in str(e):
                return False
            raise

    # WP-268 Phase 2 Phase B: dual-write notification_sent.v1.
    # Пишем ТОЛЬКО при реально новой записи (inserted=True). На дубле
    # gateway уже получил событие в первый раз — повторно не шлём, иначе
    # каждый retry catch-up scheduler'а будет дополнительно бить event-gateway.
    # PII: payload содержит metadata о уведомлении. Содержимое payload
    # caller'а МОЖЕТ содержать PII (имя пользователя в тексте) — поэтому
    # передаём только ключи, а не значения.
    if inserted:
        try:
            ory = await resolve_ory_id_from_chat(chat_id)
            asyncio.create_task(post_event(
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
            ))
        except Exception as exc:
            logger.warning(f"[Notification] dual-write schedule failed: {exc}")

    return inserted


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

    1. Записать в notification_log (log-before-send, §10.10)
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
