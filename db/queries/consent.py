"""
WP-188 Ф17: запросы к learning.tracking_consent (writer-pool consent_writer).

Контракт:
- Source-of-truth: learning.tracking_consent (миграция 109).
- Writer: роль consent_writer (миграция 113), BYPASSRLS → ОБЯЗАТЕЛЕН явный WHERE account_id.
- account_id = Ory UUID (resolve через helpers.dual_write.resolve_ory_id_from_chat).
- Scopes (default): {stage_evaluation, club_activity}.

L2-PRIVACY: см. lessons_privacy_layer2_required.md — BYPASSRLS делает RLS декоративной,
explicit SQL фильтр обязателен. Все запросы здесь параметризуют account_id.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional, TypedDict

from db.connection import get_consent_pool

logger = logging.getLogger(__name__)

DEFAULT_SCOPE = ["stage_evaluation", "club_activity"]

# In-process cache for typing_tracking consent status.
# Consent changes are rare (user toggles in /settings), so a simple dict is sufficient.
# Full clear at limit — same pattern as _ory_cache in helpers/dual_write.py.
_consent_cache: dict[str, bool] = {}
_CONSENT_CACHE_LIMIT = 5000


def invalidate_consent_cache(account_id: str) -> None:
    _consent_cache.pop(account_id, None)


class ConsentRow(TypedDict):
    account_id: str
    opt_in: bool
    scope: list[str]
    opted_at: datetime


async def get_consent(account_id: str) -> Optional[ConsentRow]:
    """Получить текущее состояние consent для account_id.

    Returns:
        ConsentRow или None, если строки нет (пользователь никогда не давал opt-in).
    """
    pool = await get_consent_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT account_id::text, opt_in, scope, opted_at "
            "FROM learning.tracking_consent "
            "WHERE account_id = $1::uuid",
            account_id,
        )
    if row is None:
        return None
    return ConsentRow(
        account_id=row["account_id"],
        opt_in=row["opt_in"],
        scope=list(row["scope"] or []),
        opted_at=row["opted_at"],
    )


async def set_consent(
    account_id: str,
    opt_in: bool = True,
    scope: Optional[list[str]] = None,
) -> ConsentRow:
    """UPSERT consent для account_id.

    GDPR-аудит: opted_at = метка ПЕРВОГО согласия, никогда не обновляется (COALESCE).
    При повторном opt-in после opt-out первая дата сохраняется как аудит-след.
    """
    pool = await get_consent_pool()
    effective_scope = scope if scope is not None else DEFAULT_SCOPE
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            -- L2-PRIVACY: account_id — PRIMARY KEY, ON CONFLICT резолвится в ту же строку
            -- по определению; cross-account запись физически невозможна (не требуется
            -- explicit WHERE для UPDATE-branch). Соответствует контракту BYPASSRLS-роли.
            --
            -- GDPR audit: opted_at — метка первого согласия. Сохраняется через COALESCE
            -- при последующих изменениях (opt-out, повторный opt-in). Если потребуется
            -- разделить «первый opt-in» и «последнее изменение» — отдельная колонка
            -- revoked_at + миграция.
            INSERT INTO learning.tracking_consent (account_id, opt_in, scope, opted_at)
            VALUES ($1::uuid, $2, $3, NOW())
            ON CONFLICT (account_id) DO UPDATE
                SET opt_in = EXCLUDED.opt_in,
                    scope = EXCLUDED.scope,
                    opted_at = COALESCE(tracking_consent.opted_at, EXCLUDED.opted_at)
            RETURNING account_id::text, opt_in, scope, opted_at
            """,
            account_id,
            opt_in,
            effective_scope,
        )
    logger.info(
        "[consent] set account_id=%s opt_in=%s scope=%s",
        account_id, opt_in, effective_scope,
    )
    return ConsentRow(
        account_id=row["account_id"],
        opt_in=row["opt_in"],
        scope=list(row["scope"] or []),
        opted_at=row["opted_at"],
    )


async def count_practice_events_30d(account_id: str) -> dict[str, int]:
    """Количество событий practice/learning за 30 дней — для понимания «насколько активен пользователь».

    Используется в /consent status: если события = 0, бот объясняет почему
    ступень в /me будет 0 даже после opt_in.

    Читаем через тот же _consent_pool — роль consent_writer имеет SELECT на
    learning.tracking_consent, но на public.domain_event нет. Нужен learning-pool.
    """
    from db.connection import get_learning_pool
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT
                COUNT(*) FILTER (WHERE activity_domain = 'practice') AS practice,
                COUNT(*) FILTER (WHERE activity_domain = 'learning') AS learning
               FROM public.domain_event
               WHERE account_id = $1::uuid
                 AND occurred_at >= NOW() - INTERVAL '30 days'""",
            account_id,
        )
    return {"practice": int(row["practice"] or 0), "learning": int(row["learning"] or 0)}


async def revoke_consent(account_id: str) -> bool:
    """GDPR right to erasure — полное удаление row.

    Returns:
        True, если row была удалена. False, если row не было.
    """
    pool = await get_consent_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM learning.tracking_consent WHERE account_id = $1::uuid",
            account_id,
        )
    deleted = result.endswith(" 1")
    logger.info("[consent] revoke account_id=%s deleted=%s", account_id, deleted)
    return deleted


# WP-316 Ф9: versioned consent_grant (learning.consent_grant)

async def get_consent_grant(account_id: str, scope: str) -> bool:
    """Проверить, есть ли активный consent для scope.

    Returns:
        True если granted=True и revoked_at IS NULL.
    """
    from db.connection import get_learning_pool
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT granted FROM learning.consent_grant
            WHERE account_id = $1::uuid AND scope = $2
              AND granted = true AND revoked_at IS NULL
            ORDER BY granted_at DESC
            LIMIT 1
            """,
            account_id,
            scope,
        )
    return row is not None and row["granted"] is True


async def set_consent_grant(
    account_id: str,
    scope: str,
    granted: bool,
    consent_version: str = "v1.0",
    interface: str = "bot",
) -> None:
    """UPSERT consent_grant для scope.

    При отзыве (granted=False) — обновляем revoked_at.
    """
    from db.connection import get_learning_pool
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        if granted:
            await conn.execute(
                """
                INSERT INTO learning.consent_grant (
                    account_id, scope, granted, consent_version, granted_at, interface
                ) VALUES ($1::uuid, $2, true, $3, NOW(), $4)
                ON CONFLICT (account_id, scope, consent_version) DO UPDATE
                    SET granted = true,
                        revoked_at = NULL,
                        granted_at = NOW()
                """,
                account_id,
                scope,
                consent_version,
                interface,
            )
        else:
            await conn.execute(
                """
                UPDATE learning.consent_grant
                SET granted = false, revoked_at = NOW()
                WHERE account_id = $1::uuid AND scope = $2
                  AND granted = true AND revoked_at IS NULL
                """,
                account_id,
                scope,
            )
    if scope == "typing_tracking":
        invalidate_consent_cache(account_id)
    logger.info(
        "[consent_grant] set account_id=%s scope=%s granted=%s",
        account_id, scope, granted,
    )

    # WP-457 Ф10 (consent write-path fix): единственный writer этого scope обязан
    # оставить отпечаток перехода в журнале (public.domain_event) — иначе grant/revoke
    # происходит без аудируемого следа. Fire-and-forget, тот же паттерн, что и
    # db/queries/events.py:log_event (legacy path не блокируется сбоем dual-write).
    try:
        from helpers.dual_write import post_event
        asyncio.create_task(post_event(
            source="aist-bot",
            external_id=f"consent-{account_id}-{scope}-{time.time_ns()}",
            event_type="consent_granted",
            schema_version="v1",
            occurred_at=datetime.utcnow(),
            account_id=account_id,
            payload={"scope": scope, "granted": granted, "consent_version": consent_version},
        ))
    except Exception as exc:
        logger.warning(f"[consent_grant] dual-write task schedule failed: {exc}")


async def is_typing_tracking_disabled(account_id: str) -> bool:
    """Пользователь явно отказался от учёта набора текста.

    Default = enabled (нет строки). True только если последняя запись = revoked (granted=false).
    granted=false всегда сопровождается revoked_at IS NOT NULL (см. set_consent_grant UPDATE path).

    Result is cached in-process; invalidated by set_consent_grant when scope='typing_tracking'.
    """
    if account_id in _consent_cache:
        return _consent_cache[account_id]

    from db.connection import get_learning_pool
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM learning.consent_grant "
            "WHERE account_id = $1::uuid AND scope = 'typing_tracking' "
            "  AND granted = false AND revoked_at IS NOT NULL "
            "ORDER BY granted_at DESC LIMIT 1",
            account_id,
        )
    result = row is not None
    if len(_consent_cache) >= _CONSENT_CACHE_LIMIT:
        _consent_cache.clear()
    _consent_cache[account_id] = result
    return result
