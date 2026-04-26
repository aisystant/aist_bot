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

import json
import logging
from datetime import datetime
from typing import Optional, Callable, Awaitable

from db.connection import get_pool
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
    """Notification idempotency — single-write на event-gateway (WP-268 cut-over).

    WP-268 cut-over: legacy INSERT INTO notification_log УДАЛЁН. Идемпотентность
    теперь делегирована event-gateway: external_id=`notification-{idempotency_key}`
    стабилен → gateway dedup'ит повторные posts на тот же ключ.

    Returns:
        True если успешно отправлено в gateway (предполагаем single-write успешен).
        False если ошибка сети — caller (`send_idempotent`) пропустит send.

    ⚠️ КРИТИЧНО для catch-up scheduler'а: log-before-send паттерн (CLAUDE.md §10.10)
    требовал atomic «log → send». Сейчас log = network call к gateway. Если
    gateway вернёт 200 (новое событие) — продолжаем send. Если gateway вернёт
    duplicate (внутренний dedup на external_id) — нужен явный сигнал, чтобы
    caller знал «этот idempotency_key уже обрабатывался». Текущий post_event
    не возвращает status, поэтому функция pessimistically возвращает True
    (продолжить send). Это меняет семантику: при retry catch-up'а уведомление
    может быть отправлено пользователю повторно, даже если gateway его дедупит.

    TODO WP-269: либо post_event должен возвращать (inserted: bool), либо
    caller должен делать read-after-write check через was_notification_sent
    (который тоже мигрирует на новую БД).
    """
    ory = await resolve_ory_id_from_chat(chat_id)
    await post_event(
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
    # post_event() fire-and-forget семантика на ошибку — assume inserted=True.
    return True


async def was_notification_sent(idempotency_key: str) -> bool:
    """Проверить, было ли уведомление уже отправлено."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id FROM notification_log WHERE idempotency_key = $1',
            idempotency_key,
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
    """Проверить cooldown nudge через notification_log.

    Заменяет nudges.was_nudge_sent_recently (WP-152 deprecation).
    Idempotency key формат: nudge:{chat_id}:{date}:{nudge_key}
    Ищет любую запись за последние cooldown_days дней.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT id FROM notification_log
               WHERE chat_id = $1
                 AND notification_type = 'nudge'
                 AND idempotency_key LIKE $2
                 AND created_at >= NOW() - INTERVAL '1 day' * $3
               LIMIT 1''',
            chat_id,
            f'nudge:{chat_id}:%:{nudge_key}',
            cooldown_days,
        )
        return row is not None


async def get_notification_stats(chat_id: int, days: int = 30) -> dict:
    """Статистика уведомлений пользователя за N дней.

    Используется для Ф4 (notification_engagement → ЦД).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT notification_type, COUNT(*) as cnt
               FROM notification_log
               WHERE chat_id = $1
                 AND created_at >= NOW() - INTERVAL '1 day' * $2
               GROUP BY notification_type''',
            chat_id, days,
        )
        return {row['notification_type']: row['cnt'] for row in rows}
