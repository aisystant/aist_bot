"""
CRUD для public.users — единый identity layer (WP-82 Phase 2).

T0: telegram_id, без ory_id.
T1+: telegram_id + ory_id (заполняется при регистрации в Ory).
"""

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from db.connection import get_pool
from helpers.dual_write import post_event

logger = logging.getLogger(__name__)


async def get_or_create_user(
    telegram_id: int,
    name: str = '',
    language: str = 'ru',
) -> dict:
    """Получить или создать запись пользователя.

    WP-268 cut-over: legacy SELECT/INSERT в public.users СОХРАНЁН — это
    bootstrap state-таблицы (Pattern 2-bootstrap), используется FSM/scheduler
    как точка идентичности до миграции state-readers (WP-269 follow-up).
    Domain event user_registered.v1 → event-gateway (single writer для домена).

    TODO WP-269: заменить INSERT на прямой write в persona.ory_identity
    (новая БД) и SELECT — на резолв через cache + новую БД.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM public.users WHERE telegram_id = $1',
            telegram_id,
        )
        if row:
            return dict(row)

        # Bootstrap row для state read path (FSM, scheduler). См. TODO выше.
        row = await conn.fetchrow('''
            INSERT INTO public.users (telegram_id, name, language)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id) DO UPDATE SET telegram_id = EXCLUDED.telegram_id
            RETURNING *
        ''', telegram_id, name, language)
        logger.info(f"[Identity] Created user for telegram_id={telegram_id}, id={row['id']}")

    # WP-268 cut-over: единственный domain writer для user_registered — event-gateway.
    # await (не create_task) — единственный путь, не fire-and-forget.
    await post_event(
        source="aist-bot",
        external_id=f"user-registered-{row['id']}",
        event_type="user_registered",
        schema_version="v1",
        occurred_at=datetime.utcnow(),
        account_id=None,  # T0 — ory_id появится через link_ory
        payload={
            "user_id": str(row['id']),
            "registration_source": "identity_get_or_create",
            "tier": "T0",
            "language": language,
        },
    )

    return dict(row)


async def get_user_by_telegram(telegram_id: int) -> Optional[dict]:
    """Получить пользователя по telegram_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM public.users WHERE telegram_id = $1',
            telegram_id,
        )
        return dict(row) if row else None


async def get_user_uuid(telegram_id: int) -> Optional[UUID]:
    """Получить UUID пользователя по telegram_id (для log_event)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id FROM public.users WHERE telegram_id = $1',
            telegram_id,
        )
        return row['id'] if row else None


async def link_ory(telegram_id: int, ory_id: str, email: Optional[str] = None) -> bool:
    """Привязать Ory UUID при переходе T0→T1.

    WP-268 cut-over: legacy UPDATE public.users УДАЛЁН. Источник истины
    для tier и ory_id — event-gateway (ory_linked.v1 + tier inferred).

    ⚠️ Без legacy UPDATE мы потеряли (а) проверку «строка существует»
    (раньше result != 'UPDATE 0' → новая привязка vs no-op); (б) syncronный
    update tier='T1' — теперь tier поднимается через projection event-gateway.
    Возвращаем True безусловно (idempotent semantics через external_id=ory_id).

    PII-инвариант: telegram_id перенесён из payload в (только) account_id resolve.
    """
    # WP-268 cut-over: единственный writer — event-gateway.
    # external_id=ory_id — идемпотентный (одна привязка на ory_id).
    now = datetime.utcnow()
    await post_event(
        source="aist-bot",
        external_id=f"ory-linked-{ory_id}",
        event_type="ory_linked",
        schema_version="v1",
        occurred_at=now,
        account_id=ory_id,
        payload={
            "tier_to": "T1",
            "email_present": bool(email),
        },
    )
    logger.info(f"[Identity] Emitted ory_linked: ory_id={ory_id} telegram_id={telegram_id}")
    return True


async def update_user_dt(telegram_id: int, dt_user_id: str) -> bool:
    """Привязать dt_user_id (Ory UUID) — single-write на event-gateway.

    WP-268 cut-over: legacy UPDATE public.users УДАЛЁН. external_id=dt_user_id
    идемпотентен (одна привязка на UUID). Возвращаем True безусловно.

    PII-инвариант: telegram_id убран из payload.
    """
    await post_event(
        source="aist-bot",
        external_id=f"dt-linked-{dt_user_id}",
        event_type="dt_linked",
        schema_version="v1",
        occurred_at=datetime.utcnow(),
        account_id=str(dt_user_id),  # dt_user_id = Ory UUID (см. CLAUDE.md §12b)
        payload={},
    )
    logger.info(f"[Identity] Emitted dt_linked: dt_user_id={dt_user_id} telegram_id={telegram_id}")
    return True


async def update_user_tier(telegram_id: int, tier: str) -> bool:
    """Изменить tier пользователя — single-write на event-gateway.

    WP-268 cut-over: legacy SELECT prev_tier + UPDATE public.users УДАЛЕНЫ.
    Без prev_tier мы не знаем «откуда» смену — payload содержит только tier_to.
    Идемпотентность через стабильный external_id (hash от telegram_id+tier).
    Repeat того же tier даст тот же external_id → gateway dedup.

    ⚠️ Если нужен tier_from для downstream (метрики апгрейдов/даунгрейдов),
    его должна вычислять projection в новой БД через diff с предыдущим event.

    PII-инвариант: telegram_id перенесён из payload в external_id seed (hash).
    """
    import hashlib as _hashlib
    # ory_id резолвим из legacy для account_id (read-only, SELECT остаётся как
    # переходный механизм; перенесётся в WP-269 на новую БД).
    pool = await get_pool()
    async with pool.acquire() as conn:
        ory_row = await conn.fetchrow(
            'SELECT ory_id::text FROM public.users WHERE telegram_id = $1',
            telegram_id,
        )
    ory_id_str = ory_row['ory_id'] if ory_row and ory_row['ory_id'] else None

    tier_hash = _hashlib.sha1(f"{telegram_id}:{tier}".encode()).hexdigest()[:12]
    await post_event(
        source="aist-bot",
        external_id=f"tier-changed-{tier_hash}",
        event_type="tier_changed",
        schema_version="v1",
        occurred_at=datetime.utcnow(),
        account_id=ory_id_str,
        payload={
            "tier_to": tier,
        },
    )
    logger.info(f"[Identity] Emitted tier_changed: telegram_id={telegram_id} tier_to={tier}")
    return True
