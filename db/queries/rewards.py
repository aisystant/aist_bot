from __future__ import annotations

"""
Чтение баланса баллов из новой Neon БД `rewards` (WP-253 Ф9.3).

Read-only: writer — projection-worker (DP.ROLE.034, DP.SC.122).
Идентификация — по `account_id` (Ory UUID).

Источник истины: `rewards.point_balances` — единственный для всех читателей
(бот, gateway-mcp /twin, Метабаза, будущий Web UI). Заменяет legacy
`payment-registry.points_*` (расхождение между legacy счётчиками устранено).
"""

import logging
from decimal import Decimal
from typing import Optional, List, Dict, Any

from db.connection import get_rewards_pool

logger = logging.getLogger(__name__)


async def get_points_balance(account_id: Optional[str]) -> Optional[Decimal]:
    """Текущий баланс баллов пользователя.

    Args:
        account_id: Ory UUID. None → не привязан, баланс отсутствует.

    Returns:
        Decimal баланс. None — пользователь без записи в point_balances
        (нет начислений) или не привязан.
    """
    if not account_id:
        return None

    try:
        pool = await get_rewards_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT points FROM point_balances WHERE account_id = $1",
                account_id,
            )
            return row['points'] if row else None
    except Exception as e:
        logger.error(f"[rewards] get_points_balance({account_id}): {e}")
        return None


async def get_recent_applied_events(
    account_id: Optional[str],
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Последние начисления баллов с разложением (для /points детализации).

    Возвращает строки `rewards.applied_events` в обратном хронологическом порядке.
    Каждая строка — событие из learning.domain_event, прошедшее через
    `compute_effective_amount()` (WP-121 Ф2 v2).

    Поля разложения: base_amount × dom_mult × qual_mult × streak_mult → effective
    (с учётом daily_cap, см. cap_truncated).
    """
    if not account_id:
        return []

    try:
        pool = await get_rewards_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, event_type, base_amount, dom_mult, qual_mult,
                       streak_mult, daily_cap, effective, cap_truncated, applied_at
                FROM applied_events
                WHERE account_id = $1
                ORDER BY applied_at DESC
                LIMIT $2
                """,
                account_id, limit,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[rewards] get_recent_applied_events({account_id}): {e}")
        return []
