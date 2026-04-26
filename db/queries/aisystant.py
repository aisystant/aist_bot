"""
DB-запросы для привязки Aisystant аккаунта (WP-79).

Хранит aisystant_id в public.users для:
- Проверки подписки БР (определяет T2)
- Запросов к Aisystant API (программы, оплата, занятия)
"""

from datetime import datetime

from db.connection import get_pool
from config import get_logger
from helpers.dual_write import post_event

logger = get_logger(__name__)


async def get_aisystant_id(chat_id: int) -> str | None:
    """Получить aisystant_id по chat_id. None если не привязан."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT aisystant_id FROM public.users WHERE telegram_id = $1',
            chat_id,
        )
        if row and row['aisystant_id']:
            return row['aisystant_id']
        return None


async def save_aisystant_link(chat_id: int, aisystant_id: str):
    """Привязка Aisystant аккаунта — single-write на event-gateway (WP-268 cut-over).

    WP-268 cut-over: УДАЛЕНЫ:
    - UPDATE public.users SET aisystant_id (legacy)
    - INSERT development.identity_map (legacy lazy write)

    Эмитим 2 события (один на linked, один на mapping для Activity Hub):
    - aisystant_linked.v1 — привязка Aisystant аккаунта
    - lms_mapping_added.v1 — Activity Hub mapping (потребляет gateway projection)

    ⚠️ user_uuid (account_id) теперь резолвим через resolve_ory_id_from_chat
    (cache + read-only legacy users). Если пользователь ещё не bootstrapped —
    account_id=None, mapping всё равно отправляется (gateway accept'ит NULL).
    """
    # Резолв identity для account_id (read-only, переходный — WP-269).
    pool = await get_pool()
    async with pool.acquire() as conn:
        user_uuid = await conn.fetchval(
            'SELECT id FROM public.users WHERE telegram_id = $1',
            chat_id,
        )

    now = datetime.utcnow()
    account_id_str = str(user_uuid) if user_uuid else None

    await post_event(
        source="aist-bot",
        external_id=f"aisystant-linked-{chat_id}-{aisystant_id}",
        event_type="aisystant_linked",
        schema_version="v1",
        occurred_at=now,
        account_id=account_id_str,
        payload={
            # PII-инвариант: telegram_id убран. aisystant_id публичный handle (LMS).
            "aisystant_id": str(aisystant_id),
        },
    )

    if user_uuid:
        await post_event(
            source="aist-bot",
            external_id=f"lms-mapping-{user_uuid}-{aisystant_id}",
            event_type="lms_mapping_added",
            schema_version="v1",
            occurred_at=now,
            account_id=account_id_str,
            payload={
                "source": "lms",
                "external_id": str(aisystant_id),
            },
        )

    logger.info(f"Aisystant linked (event-only): chat_id={chat_id}, aisystant_id={aisystant_id}")


async def remove_aisystant_link(chat_id: int):
    """Удалить привязку Aisystant — single-write на event-gateway.

    WP-268 cut-over: legacy UPDATE public.users SET aisystant_id=NULL УДАЛЁН.
    Идемпотентность через стабильный external_id (hash от chat_id+aisystant_id).
    """
    import hashlib as _hashlib
    # Резолв (read-only, переходный) — нужен для account_id и aisystant_id_was.
    pool = await get_pool()
    async with pool.acquire() as conn:
        prev = await conn.fetchrow(
            'SELECT id, aisystant_id FROM public.users WHERE telegram_id = $1',
            chat_id,
        )

    if not prev:
        logger.info(f"Aisystant unlink: chat_id={chat_id} not found, no-op")
        return

    now = datetime.utcnow()
    account_id_str = str(prev['id']) if prev.get('id') else None
    prev_aisystant_id = prev.get('aisystant_id')
    unlink_hash = _hashlib.sha1(
        f"{chat_id}:{prev_aisystant_id or ''}".encode()
    ).hexdigest()[:12]
    await post_event(
        source="aist-bot",
        external_id=f"aisystant-unlinked-{unlink_hash}",
        event_type="aisystant_unlinked",
        schema_version="v1",
        occurred_at=now,
        account_id=account_id_str,
        payload={
            "aisystant_id_was": str(prev_aisystant_id) if prev_aisystant_id else None,
        },
    )
    logger.info(f"Aisystant unlinked (event-only): chat_id={chat_id}")


async def is_aisystant_linked(chat_id: int) -> bool:
    """Проверить, привязан ли Aisystant аккаунт."""
    return await get_aisystant_id(chat_id) is not None
