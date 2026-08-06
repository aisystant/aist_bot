"""
WP-268 Phase 2 Phase A — dual-write в event-gateway параллельно с legacy DB.

# see DP.SC.NNN (event-gateway), DP.ROLE.NNN (Bot writer)

Pattern:
    После legacy INSERT/UPDATE — `asyncio.create_task(post_event(...))`,
    fire-and-forget. Если event-gateway POST упал — log warning, legacy не блокируется.

Конвенции:
    - external_id стабильный, идемпотентный (`f"{event_short}-{db_id}"`)
    - account_id = ory_id (UUID) если есть, иначе None (gateway допускает)
    - payload БЕЗ PII (только metadata: counts, ids, типы)
    - occurred_at = datetime.utcnow() (naive UTC по convention бота, см. CLAUDE.md §10.6)

Конфигурация:
    EVENT_GATEWAY_URL (env, default https://event-gateway.aisystant.workers.dev)
    EVENT_GATEWAY_TIMEOUT (env, default 5.0s)
    EVENT_GATEWAY_ENABLED (env, default true) — можно вырубить dual-write feature flag'ом

HTTP стек: aiohttp (уже в requirements.txt бота). httpx целенаправленно НЕ используется,
чтобы не добавлять транзитивную зависимость в прод-runtime.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from core.tracing import traced_acquire

from config import (
    EVENT_GATEWAY_ENABLED,
    EVENT_GATEWAY_HMAC_KEY,
    EVENT_GATEWAY_HMAC_KEY_ID,
    EVENT_GATEWAY_TIMEOUT,
    EVENT_GATEWAY_URL,
)

logger = logging.getLogger(__name__)

_session: Optional[aiohttp.ClientSession] = None

# Cache: telegram_id (chat_id) → ory_id (str) или None (нет ory привязки).
# Заполняется лениво в resolve_ory_id_from_chat(); используется high-volume
# writers'ами Phase B (qa, notifications, traces) чтобы не дёргать БД на каждое
# сообщение. Размер ограничен — clear() при превышении (см. _ORY_CACHE_LIMIT).
_ory_cache: dict = {}
_ORY_CACHE_LIMIT = 10_000


def _get_session() -> aiohttp.ClientSession:
    """Lazy-init shared aiohttp session (reuses connections across calls)."""
    global _session
    if _session is None or _session.closed:
        timeout = aiohttp.ClientTimeout(total=EVENT_GATEWAY_TIMEOUT)
        _session = aiohttp.ClientSession(timeout=timeout)
    return _session


async def aclose() -> None:
    """Закрыть HTTP-сессию при shutdown бота. Вызывать в bot.py при graceful stop."""
    global _session
    if _session is not None and not _session.closed:
        try:
            await _session.close()
        except Exception as exc:
            logger.warning(f"[dual-write] aclose failed: {exc}")
    _session = None


def _to_iso_utc(dt: datetime) -> str:
    """ISO-8601 в UTC. Naive datetime трактуется как UTC (convention бота)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


async def post_event(
    source: str,
    external_id: str,
    event_type: str,
    schema_version: str,
    occurred_at: datetime,
    account_id: Optional[str],
    payload: dict,
) -> None:
    """Fire-and-forget POST в event-gateway. На failure логирует warning, не raise.

    Args:
        source: Канал-источник, для бота всегда "aist-bot".
        external_id: Идемпотентный ID события — стабильный внутри ОДНОГО вызова
            (защищает именно от повторной доставки этого конкретного HTTP-запроса,
            не от повторных бизнес-событий: два разных grant/revoke — два разных id).
        event_type: Тип события (`user_registered`, `tier_changed`, …).
        schema_version: Версия схемы payload, например "v1".
        occurred_at: Когда событие произошло (naive UTC OK).
        account_id: Ory UUID если известен, иначе None.
        payload: Доменные данные (БЕЗ PII).
    """
    if not EVENT_GATEWAY_ENABLED:
        return

    envelope = {
        "source": source,
        "external_id": external_id,
        "event_type": event_type,
        "schema_version": schema_version,
        "occurred_at": _to_iso_utc(occurred_at),
        "payload": payload,
    }
    if account_id:
        envelope["account_id"] = account_id

    try:
        session = _get_session()
        body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json"}
        if EVENT_GATEWAY_HMAC_KEY:
            timestamp = str(int(time.time()))
            canonical = (
                f"v1\n{source}\n{EVENT_GATEWAY_HMAC_KEY_ID}\n{timestamp}\n".encode()
                + body
            )
            signature = hmac.new(
                EVENT_GATEWAY_HMAC_KEY.encode(),
                canonical,
                hashlib.sha256,
            ).hexdigest()
            headers.update({
                "X-IWE-Signature-Version": "v1",
                "X-IWE-Key-Id": EVENT_GATEWAY_HMAC_KEY_ID,
                "X-IWE-Timestamp": timestamp,
                "X-IWE-Signature": f"sha256={signature}",
            })

        async with session.post(
            f"{EVENT_GATEWAY_URL}/events",
            data=body,
            headers=headers,
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(
                    f"[dual-write] {event_type} POST failed: "
                    f"{resp.status} {body[:200]}"
                )
    except Exception as exc:
        logger.warning(f"[dual-write] {event_type} POST exception: {exc}")


async def resolve_ory_id_from_chat(chat_id: int) -> Optional[str]:
    """Cache-first lookup `chat_id (telegram_id) → ory_id (str)`.

    Используется high-volume writers'ами (qa, notifications, traces) для
    обогащения envelope account_id без блокирования основного flow.

    Returns:
        ory_id как строка (UUID) если есть. None если T0 (ещё не привязан) или
        пользователя нет в БД. На любой ошибке возвращает None и логирует
        warning (fire-and-forget семантика — не должен ломать caller).
    """
    if chat_id is None:
        return None
    # Cache hit (включая отрицательный кэш None — T0 пользователи).
    if chat_id in _ory_cache:
        return _ory_cache[chat_id]

    try:
        # Lazy import — избегаем circular dependency (db.connection → config → ...).
        # После WP-268 cut-over читаем из persona.ory_identity, не из legacy public.users.
        from db.connection import get_persona_pool
        pool = await get_persona_pool()
        async with traced_acquire(pool, "db.resolve_ory_id") as conn:
            ory = await conn.fetchval(
                "SELECT account_id::text FROM public.ory_identity WHERE telegram_id = $1",
                chat_id,
            )
    except Exception as exc:
        logger.warning(f"[dual-write] resolve_ory_id_from_chat({chat_id}) failed: {exc}")
        return None

    # Cache size management: clear полностью при превышении лимита.
    # Простой LRU не нужен: ory_id стабилен, повторное заполнение дёшево.
    if len(_ory_cache) >= _ORY_CACHE_LIMIT:
        _ory_cache.clear()
    _ory_cache[chat_id] = ory
    return ory


async def emit_payment_received(
    *,
    provider: str,
    external_payment_id: Optional[str],
    amount: float,
    currency: str,
    payment_kind_code: str,
    telegram_id: int,
    paid_at: Optional[datetime] = None,
) -> None:
    """Эмиссия сырого payment_received для любого канала оплаты (WP-266 Ф5c).

    Дальше по конвейеру: event-gateway (schema payment_received.v1, strict) →
    projection-worker rule 104 (UPSERT payment.payment_received) → hook
    first_payment.py (welcome/referral на global-first оплате). Канал НЕ
    решает «первая ли оплата» — в боте нет единой таблицы платежей
    (peer-session 2026-06-11-39), детект централизован в воркере.

    provider — строго из enum схемы шлюза: yookassa | tg_stars | aisystant
    (stripe/paybox в боте не используются). payment_kind_code — код из
    reference.payment_kind: bank_card | sbp | stars | manual (rule 104 делает
    lookup, неизвестный код = DLQ).

    Без external_payment_id идемпотентный external_id невозможен — событие
    пропускается с warning (лучше нет события, чем дубль welcome при retry).
    """
    if not external_payment_id:
        logger.warning(
            f"[payment-event] {provider} payment without external id "
            f"(tg={telegram_id}) — emission skipped"
        )
        return

    if not amount or amount <= 0:
        # Схема шлюза допускает amount=0 (minimum: 0), но CHECK(amount > 0)
        # в payment.payment_received уронит UPSERT в вечный DLQ.
        logger.warning(
            f"[payment-event] {provider} payment with amount={amount!r} "
            f"(tg={telegram_id}) — emission skipped"
        )
        return

    account_id = await resolve_ory_id_from_chat(telegram_id)
    occurred = paid_at or datetime.utcnow()
    await post_event(
        source="aist-bot",
        external_id=f"pay-rcv-{provider}-{external_payment_id}",
        event_type="payment_received",
        schema_version="v1",
        occurred_at=occurred,
        account_id=account_id,
        payload={
            "payment_id": str(uuid.uuid4()),
            "amount": float(amount),
            "currency": currency,
            "payment_kind_code": payment_kind_code,
            "external_payment_id": str(external_payment_id),
            "provider": provider,
            "paid_at": _to_iso_utc(occurred),
            "account_id_resolved": account_id,
        },
    )


def fire_event(
    source: str,
    external_id: str,
    event_type: str,
    schema_version: str,
    occurred_at: datetime,
    account_id: Optional[str],
    payload: dict,
) -> None:
    """Schedule post_event() через asyncio.create_task без await.

    Если event loop не работает — логируем warning и пропускаем (не блокирует caller).
    Использовать ИЗ async-контекста: предпочтительно прямо `asyncio.create_task(post_event(...))`.
    Этот wrapper нужен только если writer существует в edge-сценарии без активного loop.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(
                post_event(
                    source, external_id, event_type, schema_version,
                    occurred_at, account_id, payload,
                )
            )
        else:
            logger.warning(
                f"[dual-write] no running event loop, skipping {event_type} POST"
            )
    except Exception as exc:
        logger.warning(f"[dual-write] fire_event failed: {exc}")
