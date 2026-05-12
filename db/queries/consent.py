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

import logging
from datetime import datetime
from typing import Optional, TypedDict

from db.connection import get_consent_pool

logger = logging.getLogger(__name__)

DEFAULT_SCOPE = ["stage_evaluation", "club_activity"]


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

    Создаёт row или обновляет существующую. opted_at = now() при каждом изменении
    (это аудит-метка момента согласия, не первичная запись).
    """
    pool = await get_consent_pool()
    effective_scope = scope if scope is not None else DEFAULT_SCOPE
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            -- L2-PRIVACY: account_id — PRIMARY KEY, ON CONFLICT резолвится в ту же строку
            -- по определению; cross-account запись физически невозможна (не требуется
            -- explicit WHERE для UPDATE-branch). Соответствует контракту BYPASSRLS-роли.
            INSERT INTO learning.tracking_consent (account_id, opt_in, scope, opted_at)
            VALUES ($1::uuid, $2, $3, NOW())
            ON CONFLICT (account_id) DO UPDATE
                SET opt_in = EXCLUDED.opt_in,
                    scope = EXCLUDED.scope,
                    opted_at = NOW()
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
