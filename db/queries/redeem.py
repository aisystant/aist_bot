from __future__ import annotations

"""
Burn-эмиттер баллов (WP-327 Ф1+Ф2, see DP.SC.141, DP.ROLE.051).

Двухфазный коммит для зачёта баллов в оплату:
  1. reserve_burn() — резерв при чекауте (ДО YooKassa.create_payment / TG send_invoice)
  2. confirm_burn() — подтверждение при payment.succeeded webhook / successful_payment
  3. rollback_burn() — откат при payment.canceled ИЛИ timeout 30 мин

Не writer point_balances — пишет только в rewards.redeemed_events.
События эмитируются ЧЕРЕЗ event-gateway (helpers.dual_write.post_event), НЕ direct INSERT
в learning.domain_event — соответствие DP.SC.020 OwnerIntegrity (single writer = DP.ROLE.032).

Идемпотентность: PK на payment_id, INSERT ... ON CONFLICT DO NOTHING.
Late-webhook handling: confirm_burn после rollback'а → alert event 'points_redeem_late_webhook'.
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import asyncpg

from db.connection import get_rewards_pool
from db.queries.rewards import get_loyalty_rate
from helpers.dual_write import post_event

logger = logging.getLogger(__name__)

# Курс конвертации читается из loyalty_pool_config (WP-327 v4.1)
# Fallback 0.05 ₽/бонус при недоступности БД


async def _get_rate() -> Decimal:
    """Внутренний helper: rate из БД или fallback."""
    return await get_loyalty_rate()

# Timeout для автоматического rollback резерва (минут)
RESERVATION_TIMEOUT_MIN = 30

# Fallback квалификация для пилотов без записи в indicators.calculated_profile
FALLBACK_QUALIFICATION = "ученик"
FALLBACK_DAILY_CAP = Decimal("100")
FALLBACK_MULTIPLIER = Decimal("1.0")

# WP-327 Этап 20.3: использовать historic_bonus_ceiling вместо текущего daily_cap
# Баллы (earned_total) ≠ бонусы (burnable). historic_bonus_ceiling определяет,
# сколько из earned_total можно конвертировать в бонусы для скидки.
ENABLE_HISTORIC_CAP = os.getenv("ENABLE_HISTORIC_CAP", "true").lower() == "true"


async def available_discount(
    account_id: str,
    requested_amount_rub: Decimal,
    skip_ceiling: bool = False,
) -> dict:
    """Сколько скидки доступно пилоту для покупки на requested_amount_rub.

    Args:
        account_id: Ory UUID пилота
        requested_amount_rub: целевая сумма покупки в рублях
        skip_ceiling: if True — skip historic_bonus_ceiling cap; apply only balance
            and purchase-amount bounds. Used for subscription cashback where the user
            pays full price by card and bonuses are deducted afterwards (no per-transaction
            daily cap makes sense in that model).

    Returns:
        {
            "copilka_pts": Decimal,         # текущий баланс пилота
            "ceiling_pts": Decimal,         # daily_cap по квалификации (or balance when skip_ceiling)
            "available_pts": Decimal,       # min(copilka, ceiling - reserved_today, requested/rate)
            "discount_rub": Decimal,        # available_pts × POINTS_TO_RUB_RATE
            "qualification": str,           # 'ученик' / 'работник' / ... / 'общественный_деятель'
            "payable_rub": Decimal,         # requested - discount_rub
        }
    """
    pool = await get_rewards_pool()

    # Валидация входов (VR-review #3)
    requested_amount_rub = Decimal(str(requested_amount_rub))
    if requested_amount_rub <= 0:
        raise ValueError(f"requested_amount_rub must be positive, got {requested_amount_rub}")
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, AttributeError) as e:
        raise ValueError(f"Invalid account_id (not a UUID): {account_id!r}") from e

    async with pool.acquire() as conn:
        # 1. Баланс пилота + historic ceiling
        balance_row = await conn.fetchrow(
            "SELECT COALESCE(points, 0) AS balance, COALESCE(historic_bonus_ceiling, 0) AS historic_ceiling FROM public.point_balances WHERE account_id = $1",
            account_uuid,
        )
        copilka_pts = Decimal(str(balance_row["balance"])) if balance_row else Decimal("0")
        historic_ceiling = Decimal(str(balance_row["historic_ceiling"])) if balance_row else Decimal("0")

        # 2. Степень МИМ через FDW _foreign_indicators.
        # Источник: profiler R28 (DS-ai-systems/profiler/scripts/dt_calc.py:805+).
        # Шкала МИМ в profiler: Первокурсник (L1) → Ученик (L2) → Работник (L25) →
        #   Стратег (L3) → Специалист (L4) → Практик (L5) → Мастер (L6) →
        #   Реформатор (L7) → Деятель (L8).
        # Колонка qualification_level хранит INT часть L-кода (L7 → 7).
        # JSONB indicators.3_8_degree.code хранит полный код ('L7' или 'L25') когда profiler v3
        # его заполнил — приоритетный источник, точнее (нет коллизии L2 vs L25 → оба INT=2).
        qual_row = await conn.fetchrow(
            """
            SELECT qualification_level,
                   indicators->'3_derived'->'3_8_degree'->>'code' AS l_code
            FROM _foreign_indicators.calculated_profile WHERE account_id = $1
            """,
            account_uuid,
        )
        qual_level = qual_row["qualification_level"] if qual_row else None
        l_code = qual_row["l_code"] if qual_row else None

        # 3. Resolve → sort_order в _foreign_reference.qualification_multipliers (1=ученик ... 8=общ.деятель).
        # Маппинг profiler L → sort_order (см. qualification_multipliers по reference БД):
        #   L2 (Ученик)     → 1  ученик       ×1.0 cap=100
        #   L25 (Работник)  → 2  работник     ×1.3 cap=140
        #   L3 (Стратег)    → 3  стратег      ×1.6 cap=200
        #   L4 (Специалист) → 4  специалист   ×2.0 cap=280
        #   L5 (Практик)    → 5  практик      ×2.5 cap=360
        #   L6 (Мастер)     → 6  мастер       ×3.0 cap=500
        #   L7 (Реформатор) → 7  реформатор   ×4.0 cap=700
        #   L8 (Деятель)    → 8  общественный_деятель ×5.0 cap=1000
        # L05/L08/L1 (предучебные: Интересант/Определяющийся/Первокурсник) → fallback ученик.

        sort_order: int | None = None
        if l_code and l_code.startswith("L"):
            # Приоритет: JSONB code (без коллизии L2/L25)
            l_map = {
                "L2": 1, "L25": 2, "L3": 3, "L4": 4,
                "L5": 5, "L6": 6, "L7": 7, "L8": 8,
            }
            sort_order = l_map.get(l_code)
            if sort_order is None:
                logger.warning(f"[Redeem] unknown L-code {l_code!r} from JSONB → fallback")
        elif qual_level is not None:
            # Fallback: колонка INT. L2 (Ученик) и L25 (Работник) оба дают INT=2 — берём ученика (частый случай).
            # Уровни <=1 (Первокурсник, Интересант) — предучебные, fallback. 9-11 — legacy/ошибка, fallback.
            if qual_level == 2:
                sort_order = 1  # ученик (Работник редок, приближение разумное)
            elif 3 <= qual_level <= 8:
                sort_order = qual_level

        if sort_order is None:
            qualification = FALLBACK_QUALIFICATION
            ceiling_pts = FALLBACK_DAILY_CAP
        else:
            qmap_row = await conn.fetchrow(
                "SELECT qualification, daily_cap FROM _foreign_reference.qualification_multipliers WHERE sort_order = $1",
                sort_order,
            )
            if qmap_row:
                qualification = qmap_row["qualification"]
                if ENABLE_HISTORIC_CAP:
                    ceiling_pts = historic_ceiling
                else:
                    ceiling_pts = Decimal(str(qmap_row["daily_cap"]))
            else:
                logger.warning(
                    f"[Redeem] sort_order={sort_order} (from l_code={l_code!r}, level={qual_level}) not in multipliers → fallback"
                )
                qualification = FALLBACK_QUALIFICATION
                ceiling_pts = FALLBACK_DAILY_CAP if not ENABLE_HISTORIC_CAP else historic_ceiling

        # 4. Уже зарезервированное сегодня (через helper из миграции 226)
        avail_row = await conn.fetchrow(
            "SELECT public.compute_available_for_burn($1) AS available",
            account_uuid,
        )
        balance_minus_reserved = Decimal(str(avail_row["available"]))

        # 5. Effective available = min(balance_minus_reserved, ceiling, requested/rate)
        rate = await _get_rate()
        max_by_request = requested_amount_rub / rate
        if skip_ceiling:
            # Cashback model (e.g. subscription): user pays full price by card, so no
            # per-transaction daily cap applies — only available balance and purchase value.
            available_pts = min(balance_minus_reserved, max_by_request)
            ceiling_pts = balance_minus_reserved  # reflected in return dict for UI
        else:
            available_pts = min(balance_minus_reserved, ceiling_pts, max_by_request)
        available_pts = available_pts.quantize(Decimal("0.01"))

        discount_rub = (available_pts * rate).quantize(Decimal("0.01"))
        payable_rub = (requested_amount_rub - discount_rub).quantize(Decimal("0.01"))

    logger.info(
        f"[Redeem] available_discount: account={account_id[:8]}, requested={requested_amount_rub}, "
        f"qual={qualification}, balance={copilka_pts}, ceiling={ceiling_pts}, available={available_pts}, discount={discount_rub}"
    )

    return {
        "copilka_pts": copilka_pts,
        "ceiling_pts": ceiling_pts,
        "available_pts": available_pts,
        "discount_rub": discount_rub,
        "qualification": qualification,
        "payable_rub": payable_rub,
    }


async def reserve_burn(
    account_id: str,
    payment_id: str,
    points_amount: Decimal,
    payment_source: str,
    purpose: str,
    qualification_snapshot: str,
    daily_cap_snapshot: Decimal,
    expires_at: Optional[datetime] = None,
    product_code: Optional[str] = None,
) -> bool:
    """Резерв баллов перед оплатой (status='reserved').

    Двухфазный коммит: SELECT FOR UPDATE на point_balances → проверка available → INSERT.

    Args:
        account_id: Ory UUID
        payment_id: ЮКасса payment.id ИЛИ TG провизорный UUID ИЛИ "zero_{uuid}" для 100% скидки
        points_amount: сколько баллов резервируется (positive Decimal)
        payment_source: 'yookassa' / 'tg_stars' / 'stripe' / 'manual' / 'zero_payment'
        purpose: 'SEMINAR' / 'SUBSCRIPTION' / 'EVENT' / 'COURSE'
        qualification_snapshot: степень МИМ на момент резерва
        daily_cap_snapshot: daily_cap на момент резерва (для replay)
        product_code: код курса/продукта (для COURSE резервов — скопирован в confirm_course_reserves)

    Returns:
        True — резерв создан, False — payment_id уже использован (idempotent) или недостаточно баллов.
    """
    pool = await get_rewards_pool()
    points_amount = Decimal(str(points_amount))
    rate = await _get_rate()
    discount_rub = (points_amount * rate).quantize(Decimal("0.01"))

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Race protection: SELECT FOR UPDATE на point_balances. Блокирует параллельные reserve_burn для того же account_id.
            balance_row = await conn.fetchrow(
                "SELECT COALESCE(points, 0) AS balance FROM public.point_balances WHERE account_id = $1 FOR UPDATE",
                uuid.UUID(account_id),
            )
            current_balance = Decimal(str(balance_row["balance"])) if balance_row else Decimal("0")

            # Уже зарезервированное сегодня (без 'confirmed' — оно уже отражено в balance через projection)
            reserved_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(points_amount), 0) AS reserved
                FROM public.redeemed_events
                WHERE account_id = $1
                  AND reserved_at >= date_trunc('day', now())
                  AND status = 'reserved'
                """,
                uuid.UUID(account_id),
            )
            reserved_today = Decimal(str(reserved_row["reserved"]))
            available = current_balance - reserved_today

            if available < points_amount:
                logger.warning(
                    f"[Redeem] reserve_burn rejected: account={account_id[:8]}, "
                    f"requested={points_amount}, available={available} (balance={current_balance}, reserved={reserved_today})"
                )
                return False

            # INSERT с ON CONFLICT для idempotency по payment_id
            result = await conn.fetchrow(
                """
                INSERT INTO public.redeemed_events
                    (payment_id, account_id, points_amount, discount_rub,
                     qualification_snapshot, daily_cap_snapshot, payment_source, purpose, status,
                     expires_at, product_code)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'reserved', $9, $10)
                ON CONFLICT (payment_id) DO NOTHING
                RETURNING payment_id
                """,
                payment_id,
                uuid.UUID(account_id),
                points_amount,
                discount_rub,
                qualification_snapshot,
                Decimal(str(daily_cap_snapshot)),
                payment_source,
                purpose,
                expires_at,
                product_code,
            )

            if result is None:
                # ON CONFLICT — отличаем idempotent retry от race с другим резервом (VR-review #4)
                existing = await conn.fetchrow(
                    "SELECT status, account_id FROM public.redeemed_events WHERE payment_id = $1",
                    payment_id,
                )
                if existing and str(existing["account_id"]) == account_id and existing["status"] in ("reserved", "confirmed"):
                    logger.info(f"[Redeem] reserve_burn idempotent retry: payment_id={payment_id}, status={existing['status']}")
                else:
                    logger.warning(
                        f"[Redeem] reserve_burn CONFLICT (possible race or hash collision): payment_id={payment_id}, "
                        f"existing_status={existing['status'] if existing else 'unknown'}"
                    )
                return False

    logger.info(
        f"[Redeem] reserve_burn: account={account_id[:8]}, payment_id={payment_id}, "
        f"points={points_amount}, discount_rub={discount_rub}, qual={qualification_snapshot}"
    )
    return True


async def update_reserve_payment_id(provisional_id: str, real_id: str) -> bool:
    """Promote provisional yk_{uuid} reserve to a real YooKassa payment_id.

    Sets expires_at = NOW() + 7 days so rollback_expired_reservations won't touch
    the reserve during the subscription confirmation window.

    Returns True if the row was updated, False if not found (already rolled back or
    never existed).
    """
    pool = await get_rewards_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE public.redeemed_events
            SET payment_id = $1, expires_at = now() + interval '7 days'
            WHERE payment_id = $2 AND status = 'reserved'
            RETURNING payment_id
            """,
            real_id,
            provisional_id,
        )
    if row is None:
        logger.warning(
            f"[Redeem] update_reserve_payment_id: provisional_id={provisional_id} not found in status=reserved"
        )
    return row is not None


async def cancel_pending_reserve(provisional_id: str) -> bool:
    """Roll back a provisional reserve on payment creation error.

    The AND status='reserved' guard ensures this is a no-op if the reserve was
    already promoted via update_reserve_payment_id (payment_id changed to real_id).
    """
    pool = await get_rewards_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE public.redeemed_events
            SET status = 'rolled_back', rolled_back_at = now(), rollback_reason = 'payment_error'
            WHERE payment_id = $1 AND status = 'reserved'
            RETURNING payment_id
            """,
            provisional_id,
        )
    return row is not None


async def confirm_subscription_reserves(account_id: str) -> int:
    """Lazy-confirm reserved bonus points after subscription activation.

    Called in cmd_subscription when has_active_subscription() is True.  Subscription
    payments go through Aisystant, so the bot's YooKassa webhook never fires — this is
    the only confirmation path for subscription reserves.

    Only confirms reserves that already have a real payment_id (NOT LIKE 'yk_%'), so
    reserves from crashed/incomplete flows are never accidentally confirmed.

    Returns count of confirmed reserves (usually 0 or 1).
    """
    pool = await get_rewards_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE public.redeemed_events
                SET status = 'confirmed', confirmed_at = now()
                WHERE account_id = $1
                  AND purpose = 'SUBSCRIPTION'
                  AND status = 'reserved'
                  AND payment_id NOT LIKE 'yk_%'
                RETURNING payment_id, account_id, points_amount
                """,
                uuid.UUID(account_id),
            )
            for row in rows:
                await conn.execute(
                    """
                    UPDATE public.point_balances
                    SET points = points - $1, last_updated = now()
                    WHERE account_id = $2
                    """,
                    row["points_amount"],
                    row["account_id"],
                )
                logger.info(
                    f"[Redeem] confirm_subscription_reserves: payment_id={row['payment_id']}, "
                    f"points={row['points_amount']}, account={str(row['account_id'])[:8]}"
                )

    return len(rows)


async def confirm_course_reserves(account_id: str, course_codes: list[str]) -> int:
    """Lazy-confirm reserved bonus points after course access is detected.

    Called in callback_my_courses when get_user_courses() returns courses with active access.
    Course payments go through Aisystant, so the bot's YooKassa webhook never fires — this
    is the only confirmation path for COURSE reserves.

    Filters by product_code = ANY(course_codes) so a reserve for course A is never confirmed
    when the user gains access to course B via a different path.

    Returns count of confirmed reserves (usually 0 or 1 per course).
    """
    if not course_codes:
        return 0

    pool = await get_rewards_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE public.redeemed_events
                SET status = 'confirmed', confirmed_at = now()
                WHERE account_id = $1
                  AND purpose = 'COURSE'
                  AND status = 'reserved'
                  AND payment_id NOT LIKE 'yk_%'
                  AND product_code = ANY($2::text[])
                RETURNING payment_id, account_id, points_amount, product_code
                """,
                uuid.UUID(account_id),
                course_codes,
            )
            for row in rows:
                await conn.execute(
                    """
                    UPDATE public.point_balances
                    SET points = points - $1, last_updated = now()
                    WHERE account_id = $2
                    """,
                    row["points_amount"],
                    row["account_id"],
                )
                logger.info(
                    f"[Redeem] confirm_course_reserves: payment_id={row['payment_id']}, "
                    f"product_code={row['product_code']}, points={row['points_amount']}, "
                    f"account={str(row['account_id'])[:8]}"
                )

    return len(rows)


async def get_pending_course_reserves() -> list[dict]:
    """Course reserves awaiting confirmation, promoted to a real Aisystant payment_id.

    Used by the scheduler's proactive confirm-job (WP-446 Ф3b) so a course purchase
    can be confirmed without waiting for the user to reopen "Мои программы" (lazy-confirm).
    """
    pool = await get_rewards_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payment_id, account_id
            FROM public.redeemed_events
            WHERE purpose = 'COURSE'
              AND status = 'reserved'
              AND payment_id NOT LIKE 'yk_%'
            """
        )
    return [{"payment_id": r["payment_id"], "account_id": str(r["account_id"])} for r in rows]


async def confirm_reserve_by_payment_id(payment_id: str) -> Optional[Decimal]:
    """Confirm a single reserve once Aisystant reports the payment as SUCCEEDED.

    pg_advisory_xact_lock serializes this against rollback_expired_reservations' matching
    pg_try_advisory_xact_lock (same hashtext key) — without it, a reserve confirmed here
    right as its TTL rollback fires elsewhere could lose the points deduction while
    Aisystant has already granted course access (WP-446 Ф3b, peer-session 2026-07-02-15).

    Returns points_amount deducted, or None if the row was already confirmed/rolled back.
    """
    pool = await get_rewards_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"burn_reserve:{payment_id}",
            )
            row = await conn.fetchrow(
                """
                UPDATE public.redeemed_events
                SET status = 'confirmed', confirmed_at = now()
                WHERE payment_id = $1 AND status = 'reserved'
                RETURNING account_id, points_amount
                """,
                payment_id,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE public.point_balances
                SET points = points - $1, last_updated = now()
                WHERE account_id = $2
                """,
                row["points_amount"],
                row["account_id"],
            )
    logger.info(
        f"[Redeem] confirm_reserve_by_payment_id: payment_id={payment_id}, "
        f"points={row['points_amount']}, account={str(row['account_id'])[:8]}"
    )
    return row["points_amount"]


async def cleanup_expired_reserves_for_account(account_id: str) -> int:
    """Roll back expired provisional yk_ reserves for a specific account.

    Called (with try/except isolation) inside prepare_burn_offer so eligibility
    checks see a clean slate after a crash or abandoned flow.
    """
    pool = await get_rewards_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE public.redeemed_events
            SET status = 'rolled_back', rolled_back_at = now(), rollback_reason = 'provisional_expired'
            WHERE account_id = $1
              AND status = 'reserved'
              AND payment_id LIKE 'yk_%'
              AND expires_at IS NOT NULL
              AND expires_at < now()
            RETURNING payment_id
            """,
            uuid.UUID(account_id),
        )
    if rows:
        logger.info(
            f"[Redeem] cleanup_expired_reserves_for_account: rolled back {len(rows)} provisional reserve(s) "
            f"for account={account_id[:8]}"
        )
    return len(rows)


async def confirm_burn(payment_id: str) -> bool:
    """Подтверждение резерва после успешной оплаты.

    Args:
        payment_id: ID платежа (из webhook payment.succeeded ИЛИ TG telegram_payment_charge_id)

    Returns:
        True — подтверждено (или уже было подтверждено идемпотентно),
        False — payment_id не найден ИЛИ был rolled_back (late-webhook).
    """
    pool = await get_rewards_pool()

    async with pool.acquire() as conn:
        # ВАЖНО: confirm разделён на ДВЕ транзакции для предотвращения webhook retry-loop
        # при CHECK (points >= 0) violation.
        # Tx1: UPDATE redeemed_events status='confirmed' — committed независимо.
        # Tx2: UPDATE point_balances — если CHECK violation, status уже 'confirmed' и
        # webhook вернёт 200 (не будет ретрая ЮКассы). Admin alert через post_event.
        # (VR-review C-блокер #1, 17 мая 2026)

        row = await conn.fetchrow(
            """
            UPDATE public.redeemed_events
            SET status = 'confirmed', confirmed_at = now()
            WHERE payment_id = $1 AND status = 'reserved'
            RETURNING account_id, points_amount
            """,
            payment_id,
        )

        if row is None:
            # Не нашли в status='reserved' — проверяем текущий статус (идемпотентность / late-webhook)
            existing = await conn.fetchrow(
                "SELECT status, account_id, points_amount FROM public.redeemed_events WHERE payment_id = $1",
                payment_id,
            )

            if existing is None:
                logger.warning(f"[Redeem] confirm_burn: payment_id={payment_id} not found")
                return False

            if existing["status"] == "confirmed":
                logger.info(f"[Redeem] confirm_burn idempotent: payment_id={payment_id} already confirmed")
                return True

            if existing["status"] == "rolled_back":
                logger.error(
                    f"[Redeem] LATE WEBHOOK: payment_id={payment_id} was rolled_back, but payment succeeded. "
                    f"Admin alert sent via event-gateway. Manual review required."
                )
                asyncio.create_task(
                    post_event(
                        source="aist-bot",
                        external_id=f"redeem_late_webhook_{payment_id}",
                        event_type="points_redeem_late_webhook",
                        schema_version="v1",
                        occurred_at=datetime.now(timezone.utc),
                        account_id=str(existing["account_id"]),
                        payload={
                            "payment_id": payment_id,
                            "points_amount": str(existing["points_amount"]),
                            "issue": "payment_succeeded_after_rollback",
                        },
                    )
                )
                return False

            logger.warning(f"[Redeem] confirm_burn unexpected status: {existing['status']}")
            return False

        # row is not None: статус успешно переведён в 'confirmed'. Tx2: inline UPDATE point_balances.
        # TODO (Phase 2 refactor): убрать inline UPDATE и перейти на projection-worker handler для
        # event_type='points_redeemed'. См. DP.ROLE.051 §9.
        try:
            await conn.execute(
                """
                UPDATE public.point_balances
                SET points = points - $1, last_updated = now()
                WHERE account_id = $2
                """,
                row["points_amount"],
                row["account_id"],
            )
        except asyncpg.CheckViolationError as e:
            # CHECK (points >= 0) — баланс изменился между reserve и confirm. status уже 'confirmed'
            # (Tx1), webhook возвращает 200, ретрая ЮКассы нет. Admin alert для ручного refund.
            logger.error(
                f"[Redeem] confirm_burn negative_balance: payment_id={payment_id}, "
                f"points={row['points_amount']}, account={str(row['account_id'])[:8]}. "
                f"Status set to 'confirmed', balance NOT decremented. Admin review needed. Error: {e}"
            )
            asyncio.create_task(
                post_event(
                    source="aist-bot",
                    external_id=f"redeem_negative_{payment_id}",
                    event_type="points_redeem_negative_balance",
                    schema_version="v1",
                    occurred_at=datetime.now(timezone.utc),
                    account_id=str(row["account_id"]),
                    payload={
                        "payment_id": payment_id,
                        "points_amount": str(row["points_amount"]),
                        "issue": "check_violation_balance_below_zero",
                    },
                )
            )

        # Успешный confirm — эмитируем event для projection-worker (read-replica на point_balances)
        asyncio.create_task(
            post_event(
                source="aist-bot",
                external_id=f"redeem_{payment_id}",
                event_type="points_redeemed",
                schema_version="v1",
                occurred_at=datetime.now(timezone.utc),
                account_id=str(row["account_id"]),
                payload={
                    "payment_id": payment_id,
                    "points_amount": str(row["points_amount"]),
                },
            )
        )

    logger.info(f"[Redeem] confirm_burn: payment_id={payment_id}, points={row['points_amount']}")
    return True


async def rollback_burn(payment_id: str, reason: str = "manual") -> bool:
    """Откат резерва.

    Args:
        payment_id: ID платежа
        reason: 'payment_canceled' / 'timeout_30min' / 'manual'

    Returns:
        True — откачено, False — не найдено или не в status='reserved'.
    """
    pool = await get_rewards_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE public.redeemed_events
            SET status = 'rolled_back', rolled_back_at = now(), rollback_reason = $2
            WHERE payment_id = $1 AND status = 'reserved'
            RETURNING account_id, points_amount
            """,
            payment_id,
            reason,
        )

    if row is None:
        logger.info(f"[Redeem] rollback_burn no-op: payment_id={payment_id} not in 'reserved'")
        return False

    # Reverse event для audit (projection-worker НЕ обрабатывает — баланс не менялся)
    asyncio.create_task(
        post_event(
            source="aist-bot",
            external_id=f"redeem_rollback_{payment_id}",
            event_type="points_burn_rolled_back",
            schema_version="v1",
            occurred_at=datetime.now(timezone.utc),
            account_id=str(row["account_id"]),
            payload={
                "payment_id": payment_id,
                "points_amount": str(row["points_amount"]),
                "reason": reason,
            },
        )
    )

    logger.info(f"[Redeem] rollback_burn: payment_id={payment_id}, points={row['points_amount']}, reason={reason}")
    return True


async def rollback_expired_reservations() -> int:
    """Cron-задача: откатывает резервы старше RESERVATION_TIMEOUT_MIN минут (или собственного
    expires_at — так же покрывает 7-дневные резервы курсов/подписки после промоушена к
    реальному payment_id, см. update_reserve_payment_id).

    pg_try_advisory_xact_lock (тот же hashtext-ключ, что confirm_reserve_by_payment_id) —
    защита от отката резерва, который в этот момент подтверждается конкурентно (WP-446 Ф3b,
    peer-session 2026-07-02-15): если лок занят — строка просто не попадает в выборку,
    откат повторится в следующем цикле.

    Returns:
        Количество откаченных резервов.
    """
    pool = await get_rewards_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE public.redeemed_events
            SET status = 'rolled_back', rolled_back_at = now(), rollback_reason = $1
            WHERE status = 'reserved'
              AND COALESCE(expires_at, reserved_at + $2 * INTERVAL '1 minute') < now()
              AND pg_try_advisory_xact_lock(hashtext('burn_reserve:' || payment_id))
            RETURNING payment_id, account_id, points_amount
            """,
            f"timeout_{RESERVATION_TIMEOUT_MIN}min",
            RESERVATION_TIMEOUT_MIN,
        )

    for row in rows:
        asyncio.create_task(
            post_event(
                source="aist-bot",
                external_id=f"redeem_rollback_{row['payment_id']}",
                event_type="points_burn_rolled_back",
                schema_version="v1",
                occurred_at=datetime.now(timezone.utc),
                account_id=str(row["account_id"]),
                payload={
                    "payment_id": row["payment_id"],
                    "points_amount": str(row["points_amount"]),
                    "reason": f"timeout_{RESERVATION_TIMEOUT_MIN}min",
                },
            )
        )

    count = len(rows)
    if count > 0:
        logger.info(f"[Redeem] rollback_expired_reservations: rolled back {count} reservations (>{RESERVATION_TIMEOUT_MIN}min)")
    return count


async def get_redeem_history(account_id: str, limit: int = 10) -> list[dict]:
    """История списаний пилота для UI /points history.

    Returns: список dict'ов с полями payment_id, points_amount, discount_rub,
    purpose, status, reserved_at, confirmed_at, rolled_back_at.
    """
    pool = await get_rewards_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payment_id, points_amount, discount_rub, purpose, payment_source,
                   status, qualification_snapshot, reserved_at, confirmed_at, rolled_back_at, rollback_reason
            FROM public.redeemed_events
            WHERE account_id = $1
            ORDER BY reserved_at DESC
            LIMIT $2
            """,
            uuid.UUID(account_id),
            limit,
        )

    return [dict(row) for row in rows]
