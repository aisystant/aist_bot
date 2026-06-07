from __future__ import annotations

"""
DB-запросы для привязки Aisystant аккаунта (WP-79).

Read-path (WP-269): persona.ory_identity.traits->>'aisystant_id' через PERSONA_URL.
Write-path (WP-268 Phase B): event-gateway POST (aisystant_linked / aisystant_unlinked /
lms_mapping_added) + legacy UPDATE для backward-совместимости (postcutover можно убрать).
"""

import asyncio
from datetime import datetime

from db.connection import get_pool, get_persona_pool
from config import get_logger
from helpers.dual_write import post_event

logger = get_logger(__name__)


async def get_aisystant_id(chat_id: int) -> str | None:
    """Получить aisystant_id по chat_id. None если не привязан.

    WP-269 Ф1: чтение только из persona.ory_identity (backfill завершён 28 апр, 0 строк pending).
    COALESCE покрывает оба ключа ETL: aisystant_suser_id (WP-268) и aisystant_id (alias).
    """
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT COALESCE(traits->>'aisystant_suser_id', traits->>'aisystant_id')
                   AS aisystant_id FROM ory_identity WHERE telegram_id = $1""",
                chat_id,
            )
            if row and row['aisystant_id']:
                return row['aisystant_id']
    except Exception as e:
        logger.warning(f"[Aisystant] persona.ory_identity read failed: {e}")

    return None


async def save_aisystant_link(chat_id: int, aisystant_id: str):
    """Сохранить привязку Aisystant аккаунта.

    Также пишет маппинг в development.identity_map (lazy write)
    для Activity Hub (WP-109 Ф1). identity_map → crm.identity_links при WP-183.

    WP-188 Ф17 фикс: дополнительно UPDATE persona.ory_identity.telegram_id —
    иначе resolve_ory_id_from_chat возвращает None для пользователей, чей
    ory_identity-row ETL'ом уже создан, но telegram_id не проставлен (9299/9896 на 12 мая).
    Без этого /consent и любой другой код, требующий Ory UUID, падает после /link.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            '''UPDATE public.users
               SET aisystant_id = $2,
                   aisystant_linked_at = NOW(),
                   updated_at = NOW()
               WHERE telegram_id = $1''',
            chat_id, aisystant_id,
        )
        logger.info(f"[Aisystant] UPDATE result: {result} for chat_id={chat_id}")

    # Сбрасываем negative-cache resolve_ory_id_from_chat ДО UPDATE — устраняет race-окно,
    # когда параллельный запрос мог бы закэшировать None ПОСЛЕ UPDATE но ДО pop.
    try:
        from helpers.dual_write import _ory_cache
        _ory_cache.pop(chat_id, None)
    except Exception:
        pass

    # WP-188 Ф17: завершаем мост tg ↔ ory — пишем telegram_id в persona.ory_identity,
    # чтобы resolve_ory_id_from_chat работал сразу после /link.
    #
    # LIMIT 1 через CTE — защита от теоретического дубликата aisystant_id в ETL.
    # Без LIMIT множественный UPDATE приведёт к коллизии: один chat_id → 2 ory UUID,
    # resolve_ory_id_from_chat вернёт недетерминированный, consent пойдёт не туда.
    # Если matches > 1 — логируем для аудита (предпочитаем точное поле aisystant_suser_id).
    try:
        persona_pool = await get_persona_pool()
        async with persona_pool.acquire() as pconn:
            collision_count = await pconn.fetchval(
                """SELECT COUNT(*) FROM public.ory_identity
                   WHERE (traits->>'aisystant_suser_id' = $1 OR traits->>'aisystant_id' = $1)""",
                str(aisystant_id),
            )
            if collision_count and collision_count > 1:
                logger.warning(
                    f"[Aisystant] persona.ory_identity COLLISION: aisystant={aisystant_id} "
                    f"matches {collision_count} rows; will update only 1 (preferring aisystant_suser_id)"
                )
            persona_result = await pconn.execute(
                """WITH target AS (
                       SELECT account_id FROM public.ory_identity
                       WHERE (traits->>'aisystant_suser_id' = $2 OR traits->>'aisystant_id' = $2)
                         AND (telegram_id IS NULL OR telegram_id <> $1)
                       ORDER BY (traits->>'aisystant_suser_id' = $2) DESC
                       LIMIT 1
                   )
                   UPDATE public.ory_identity oi
                   SET telegram_id = $1
                   FROM target
                   WHERE oi.account_id = target.account_id""",
                chat_id, str(aisystant_id),
            )
            logger.info(f"[Aisystant] persona.ory_identity UPDATE: {persona_result} for chat_id={chat_id} aisystant={aisystant_id}")

            # WP-327 fix: sync Ory UUID → public.users.dt_user_id so /points and 6 other
            # handlers work immediately after /link without a separate DT OAuth flow.
            ory_uuid = await pconn.fetchval(
                """SELECT account_id FROM public.ory_identity
                   WHERE (traits->>'aisystant_suser_id' = $1 OR traits->>'aisystant_id' = $1)
                   ORDER BY (traits->>'aisystant_suser_id' = $1) DESC
                   LIMIT 1""",
                str(aisystant_id),
            )
            if ory_uuid:
                from db.queries.identity import update_user_dt
                await update_user_dt(chat_id, str(ory_uuid))
                logger.info(f"[Aisystant] synced dt_user_id={ory_uuid} for chat_id={chat_id}")
    except Exception as exc:
        # Не блокируем /link — лучше успешная привязка с задержкой ory-моста,
        # чем падение /link целиком. Без telegram_id в persona /consent выведет
        # «синхронизация идёт» (graceful degrade в handlers/consent.py).
        logger.warning(f"[Aisystant] persona.ory_identity UPDATE failed: {exc}")

    # Двойной pop: после UPDATE гарантируем, что race-запрос (между UPDATE и pop ДО),
    # который успел закэшировать ещё-None, не оставит stale значение.
    try:
        from helpers.dual_write import _ory_cache
        _ory_cache.pop(chat_id, None)
    except Exception:
        pass

    # Lazy write в identity_map для Activity Hub (WP-109).
    # ФИКС (peer-session 2026-06-05-02): блок был ошибочно вложен в `except: pass`
    # выше (отступ 8 пробелов) → на нормальном пути не исполнялся, а user_uuid
    # оставался несвязанным → UnboundLocalError на account_id_str ниже. Плюс conn
    # из `async with` выше уже закрыт. Решение: свежее соединение + init user_uuid=None.
    user_uuid = None
    try:
        async with pool.acquire() as conn2:
            user_uuid = await conn2.fetchval(
                'SELECT id FROM public.users WHERE telegram_id = $1',
                chat_id,
            )
            logger.info(f"[Aisystant] user_uuid from SELECT: {user_uuid}")
            if user_uuid:
                await conn2.execute(
                    '''INSERT INTO development.identity_map (source, external_id, user_uuid)
                       VALUES ('lms', $1, $2)
                       ON CONFLICT (source, external_id) DO NOTHING''',
                    str(aisystant_id), user_uuid,
                )
                logger.info(f"[Aisystant] identity_map INSERT for {user_uuid}")
    except Exception as exc:
        logger.warning(f"[Aisystant] identity_map lazy-write failed: {exc}")

    logger.info(f"[Aisystant] linked: chat_id={chat_id}, aisystant_id={aisystant_id}")

    # WP-268 Phase 2 dual-write: 2 события на одну операцию
    # (a) aisystant_linked — привязка Aisystant аккаунта к нашему user
    # (b) lms_mapping_added — маппинг в identity_map для Activity Hub
    now = datetime.utcnow()
    account_id_str = str(user_uuid) if user_uuid else None

    asyncio.create_task(post_event(
        source="aist-bot",
        external_id=f"aisystant-linked-{chat_id}-{aisystant_id}",
        event_type="aisystant_linked",
        schema_version="v1",
        occurred_at=now,
        account_id=account_id_str,
        payload={
            "aisystant_id": str(aisystant_id),
        },
    ))

    if user_uuid:
        asyncio.create_task(post_event(
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
        ))


async def remove_aisystant_link(chat_id: int):
    """Удалить привязку Aisystant аккаунта."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Берём user_uuid + предыдущий aisystant_id ДО апдейта
        prev = await conn.fetchrow(
            'SELECT id, aisystant_id FROM public.users WHERE telegram_id = $1',
            chat_id,
        )
        await conn.execute(
            '''UPDATE public.users
               SET aisystant_id = NULL,
                   aisystant_linked_at = NULL,
                   updated_at = NOW()
               WHERE telegram_id = $1''',
            chat_id,
        )
    logger.info(f"Aisystant unlinked: chat_id={chat_id}")

    # WP-268 Phase 2 dual-write: aisystant_unlinked
    if prev:
        now = datetime.utcnow()
        account_id_str = str(prev['id']) if prev.get('id') else None
        prev_aisystant_id = prev.get('aisystant_id')
        asyncio.create_task(post_event(
            source="aist-bot",
            external_id=f"aisystant-unlinked-{chat_id}-{int(now.timestamp() * 1_000_000_000)}",
            event_type="aisystant_unlinked",
            schema_version="v1",
            occurred_at=now,
            account_id=account_id_str,
            payload={
                "aisystant_id_was": str(prev_aisystant_id) if prev_aisystant_id else None,
            },
        ))


async def is_aisystant_linked(chat_id: int) -> bool:
    """Проверить, привязан ли Aisystant аккаунт."""
    return await get_aisystant_id(chat_id) is not None
