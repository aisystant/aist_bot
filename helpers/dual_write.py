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
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from config import EVENT_GATEWAY_URL, EVENT_GATEWAY_TIMEOUT, EVENT_GATEWAY_ENABLED

logger = logging.getLogger(__name__)

_session: Optional[aiohttp.ClientSession] = None


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
        external_id: Идемпотентный ID события (стабильный для retry).
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
        async with session.post(f"{EVENT_GATEWAY_URL}/events", json=envelope) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(
                    f"[dual-write] {event_type} POST failed: "
                    f"{resp.status} {body[:200]}"
                )
    except Exception as exc:
        logger.warning(f"[dual-write] {event_type} POST exception: {exc}")


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
