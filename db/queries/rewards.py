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

from db.connection import get_rewards_pool, get_reference_pool

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


async def get_today_raw_total(account_id: Optional[str]) -> Decimal:
    """Сумма raw (base×dom×qual×streak, без daily_cap) за сегодня — баллы без ограничений."""
    if not account_id:
        return Decimal(0)

    try:
        pool = await get_rewards_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(
                    SUM(base_amount * dom_mult * qual_mult * streak_mult), 0
                ) AS today_raw
                FROM applied_events
                WHERE account_id = $1
                  AND DATE(applied_at) = CURRENT_DATE
                """,
                account_id,
            )
            return row['today_raw'] if row else Decimal(0)
    except Exception as e:
        logger.error(f"[rewards] get_today_raw_total({account_id}): {e}")
        return Decimal(0)


async def get_today_total(account_id: Optional[str]) -> Decimal:
    """Сумма effective (бонусы) за сегодня (UTC). Для /points прогресс-бара бонусов."""
    if not account_id:
        return Decimal(0)

    try:
        pool = await get_rewards_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(effective), 0) AS today_total
                FROM applied_events
                WHERE account_id = $1
                  AND DATE(applied_at) = CURRENT_DATE
                """,
                account_id,
            )
            return row['today_total'] if row else Decimal(0)
    except Exception as e:
        logger.error(f"[rewards] get_today_total({account_id}): {e}")
        return Decimal(0)


async def get_active_reward_rules() -> List[Dict[str, Any]]:
    """Список активных правил для команды /rules (DP.SC.136).

    Возвращает только правила с amount > 0 (условия с amount=0 — служебные,
    не отображаются пилоту). Группировка по activity_domain — на стороне handler.
    """
    try:
        pool = await get_reference_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT trigger_event, amount, streak_eligible, description,
                       effort_minutes, is_marker, max_per_day, group_mult, rarity_mult
                FROM reward_rules
                WHERE reward_kind = 'points'
                  AND amount > 0
                  AND (valid_to IS NULL OR valid_to > NOW())
                ORDER BY group_mult DESC, amount DESC, trigger_event
                """,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[rewards] get_active_reward_rules: {e}")
        return []


async def get_domain_multipliers() -> Dict[str, Dict[str, Any]]:
    """Множители по доменам активности (DP.SC.136 /rules объяснение).

    Returns: {'learning': {multiplier, daily_cap_default}, 'practice': ..., 'work': ...}
    """
    try:
        pool = await get_reference_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT domain, multiplier, daily_cap_default FROM activity_domain_multipliers ORDER BY domain"
            )
            return {
                r['domain']: {
                    'multiplier': float(r['multiplier']),
                    'daily_cap_default': float(r['daily_cap_default']),
                }
                for r in rows
            }
    except Exception as e:
        logger.error(f"[rewards] get_domain_multipliers: {e}")
        return {}


async def get_earned_total(account_id: Optional[str]) -> Optional[Decimal]:
    """Earned-total = накопленные + потраченные confirmed бонусы (монотонный счётчик).

    Вычисляется как point_balances.points + SUM(redeemed_events.points_amount WHERE confirmed).
    Никогда не убывает — показывается как «Заработано всего» (DP.D.050).
    """
    if not account_id:
        return None

    try:
        pool = await get_rewards_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(
                        (SELECT points FROM point_balances WHERE account_id = $1),
                        0
                    ) + COALESCE(
                        (SELECT SUM(points_amount) FROM redeemed_events
                         WHERE account_id = $1 AND status = 'confirmed'),
                        0
                    ) AS earned_total
                """,
                account_id,
            )
            return row['earned_total'] if row else None
    except Exception as e:
        logger.error(f"[rewards] get_earned_total({account_id}): {e}")
        return None


async def get_recent_redeemed_events(account_id: Optional[str], limit: int = 3) -> list:
    """Последние confirmed списания (burn-операции) пилота.

    WP-188 Ф17 #4: история списаний в /points UI. Источник: rewards.redeemed_events
    (WP-327 Ф1, two-phase commit). Только status='confirmed' — резервы и откаты не показываем.
    """
    if not account_id:
        return []

    try:
        pool = await get_rewards_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT payment_id, points_amount, discount_rub, purpose, payment_source,
                       confirmed_at, reserved_at
                FROM public.redeemed_events
                WHERE account_id = $1 AND status = 'confirmed'
                ORDER BY confirmed_at DESC NULLS LAST
                LIMIT $2
                """,
                account_id,
                limit,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[rewards] get_recent_redeemed_events({account_id}): {e}")
        return []


async def get_user_daily_cap(account_id: Optional[str]) -> Optional[int]:
    """Суточный потолок бонусов = action_cap × K (читаем из qualification_levels_v4 и loyalty_pool_config)."""
    if not account_id:
        return None

    try:
        pool = await get_rewards_pool()
        async with pool.acquire() as conn:
            # Primary: action_cap × K (единый источник истины)
            row = await conn.fetchrow(
                """
                SELECT COALESCE(ql.action_cap, 200) * COALESCE(lpc.K, 10) AS daily_total_cap
                FROM _foreign_indicators.calculated_profile cp
                LEFT JOIN _foreign_reference.qualification_levels_v4 ql
                    ON ql.level_number = COALESCE(
                        CASE
                            WHEN cp.qualification_level BETWEEN 1 AND 3 THEN cp.qualification_level
                            WHEN cp.qualification_level = 4 THEN COALESCE((cp.rcs_current ->> 'stage')::INT, 5)
                            WHEN cp.qualification_level BETWEEN 5 AND 11 THEN cp.qualification_level + 1
                            ELSE 5
                        END, 5)
                CROSS JOIN (
                    SELECT K FROM _foreign_reference.loyalty_pool_config
                    WHERE valid_to IS NULL
                    ORDER BY valid_from DESC
                    LIMIT 1
                ) lpc
                WHERE cp.account_id = $1
                """,
                account_id,
            )
            if row and row['daily_total_cap']:
                return int(row['daily_total_cap'])

            # Fallback: последнее событие с разумным daily_cap (для пользователей без calculated_profile)
            row = await conn.fetchrow(
                """
                SELECT daily_cap AS daily_total_cap
                FROM applied_events
                WHERE account_id = $1 AND daily_cap >= 20
                ORDER BY applied_at DESC
                LIMIT 1
                """,
                account_id,
            )
            if row and row['daily_total_cap']:
                return int(row['daily_total_cap'])

            return 2000  # default = 200 × 10
    except Exception as e:
        logger.error(f"[rewards] get_user_daily_cap({account_id}): {e}")
        return None


async def get_loyalty_rate() -> Decimal:
    """Текущий курс бонусов из loyalty_pool_config (WP-327 v4.1).

    Returns: rate в ₽/бонус. Default 0.05 если конфиг не найден.
    """
    try:
        pool = await get_reference_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT rate FROM loyalty_pool_config
                WHERE valid_to IS NULL OR valid_to > NOW()
                ORDER BY valid_from DESC
                LIMIT 1
                """
            )
            return Decimal(str(row['rate'])) if row and row['rate'] else Decimal("0.05")
    except Exception as e:
        logger.error(f"[rewards] get_loyalty_rate: {e}")
        return Decimal("0.05")


async def get_user_action_cap(account_id: Optional[str]) -> Optional[int]:
    """Action cap пользователя по unified 12-level справочнику (WP-327 v4.1)."""
    if not account_id:
        return None
    try:
        pool = await get_rewards_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ql.action_cap
                FROM _foreign_reference.qualification_levels_v4 ql
                JOIN _foreign_indicators.calculated_profile cp ON cp.account_id = $1
                WHERE ql.level_number = COALESCE(
                    CASE
                        WHEN cp.qualification_level BETWEEN 1 AND 3 THEN cp.qualification_level
                        WHEN cp.qualification_level = 4 THEN (cp.rcs_current ->> 'stage')::INT
                        WHEN cp.qualification_level BETWEEN 5 AND 11 THEN cp.qualification_level + 1
                        ELSE 5
                    END, 5)
                """,
                account_id,
            )
            return int(row['action_cap']) if row and row['action_cap'] else 200
    except Exception as e:
        logger.error(f"[rewards] get_user_action_cap({account_id}): {e}")
        return 200


async def get_student_stage_multipliers() -> List[Dict[str, Any]]:
    """Ступени Ученика (1-5) с именами, множителями и потолками из reference БД."""
    try:
        pool = await get_reference_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT stage, name, multiplier, daily_cap
                FROM student_stage_multipliers
                ORDER BY stage
                """
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[rewards] get_student_stage_multipliers: {e}")
        return []


async def get_qualification_multipliers_list() -> List[Dict[str, Any]]:
    """Квалификации МИМ (8 уровней: Ученик → Общественный деятель) из reference БД.

    Таблица WP-268 Phase 2: PK = qualification (TEXT lowercase Russian),
    нет колонки sort_order — сортируем по multiplier (прокси порядка уровней).
    """
    try:
        pool = await get_reference_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT qualification, multiplier, daily_cap
                FROM qualification_multipliers
                ORDER BY multiplier
                """
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[rewards] get_qualification_multipliers_list: {e}")
        return []


async def get_qualification_levels_v4_list() -> List[Dict[str, Any]]:
    """12 уровней квалификации из qualification_levels_v4 (WP-327 v4.1).

    Returns: список словарей с полями level_number, code, name, qual_mult,
             action_cap, daily_cap_bonuses (computed as action_cap × K).
    """
    try:
        pool = await get_reference_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    ql.level_number,
                    ql.code,
                    ql.name,
                    ql.qual_mult,
                    ql.action_cap,
                    ql.action_cap * COALESCE(lpc.K, 10) AS daily_cap_bonuses
                FROM qualification_levels_v4 ql
                CROSS JOIN (
                    SELECT K FROM loyalty_pool_config
                    WHERE valid_to IS NULL
                    ORDER BY valid_from DESC
                    LIMIT 1
                ) lpc
                ORDER BY ql.level_number
                """
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[rewards] get_qualification_levels_v4_list: {e}")
        return []
