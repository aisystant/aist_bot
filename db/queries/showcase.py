from __future__ import annotations

"""
DB-запросы для витрины семинаров (WP-5).

products (type='seminar') — Neon reference.product (DP.ARCH.004 §3.8).
finance_payments          — Railway /bot_data (tech debt bridge до G3 → payment BC).
"""

from db.connection import get_bot_data_pool, get_reference_pool
from config import get_logger

logger = get_logger(__name__)


# ── products (каталог семинаров) — Neon reference ───────────


async def get_active_seminars(*, free_only: bool = False) -> list[dict]:
    """Получить активные семинары из reference.product, отсортированные по sort_order."""
    pool = await get_reference_pool()
    async with pool.acquire() as conn:
        if free_only:
            rows = await conn.fetch(
                """SELECT * FROM public.product
                   WHERE type = 'seminar' AND active = TRUE AND is_free = TRUE
                   ORDER BY sort_order""",
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM public.product
                   WHERE type = 'seminar' AND active = TRUE
                   ORDER BY sort_order""",
            )
    return [dict(r) for r in rows]


async def get_seminar_by_code(code: str) -> dict | None:
    """Получить семинар по code (PK)."""
    pool = await get_reference_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM public.product WHERE code = $1 AND type = 'seminar' AND active = TRUE",
            code,
        )
    return dict(row) if row else None


async def get_seminar_by_tilda_uid(tilda_uid: str) -> dict | None:
    """Получить семинар по tilda_uid (в metadata)."""
    pool = await get_reference_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM public.product
               WHERE type = 'seminar' AND active = TRUE
                 AND metadata->>'tilda_uid' = $1""",
            tilda_uid,
        )
    return dict(row) if row else None


# ── finance_payments — Railway /bot_data (tech debt bridge) ─


async def create_seminar_payment(
    telegram_id: int,
    product_code: str,
    amount: float,
    currency: str = "RUB",
    *,
    source: str = "bot",
    payment_id: str | None = None,
) -> int:
    """Записать оплату семинара в finance_payments. Возвращает id."""
    pool = await get_bot_data_pool()
    channel = 9 if currency == "XTR" else 10

    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            """INSERT INTO public.finance_payments
                   (telegram_id, code, amount, currency, channel, success,
                    purpose, ext_id, created_at)
               VALUES ($1, $2, $3, $4, $5, TRUE, 'SEMINAR', $6, NOW())
               ON CONFLICT (ext_id) WHERE ext_id IS NOT NULL DO NOTHING
               RETURNING id""",
            telegram_id, product_code, amount, currency, channel, payment_id,
        )
    if row_id is None:
        logger.info(f"[Showcase] duplicate payment ext_id={payment_id}, skipped")
        return 0
    logger.info(f"[Showcase] payment: id={row_id}, tg={telegram_id}, product={product_code}, amount={amount} {currency}")
    return row_id


async def has_seminar_access(telegram_id: int, product_code: str) -> bool:
    """Проверить, оплачен ли семинар пользователем."""
    pool = await get_bot_data_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM public.finance_payments
               WHERE telegram_id = $1 AND code = $2 AND success = TRUE""",
            telegram_id, product_code,
        )
    return (count or 0) > 0


async def has_access_to_chat(telegram_id: int, chat_id: int) -> bool:
    """Проверить, есть ли у пользователя оплаченный семинар с данным chat_id."""
    # Шаг 1: найти codes семинаров с нужным tg_chat_id в Neon reference
    ref_pool = await get_reference_pool()
    async with ref_pool.acquire() as conn:
        codes = await conn.fetch(
            "SELECT code FROM public.product WHERE tg_chat_id = $1 AND type = 'seminar' AND active = TRUE",
            chat_id,
        )
    if not codes:
        return False
    seminar_codes = [r["code"] for r in codes]

    # Шаг 2: проверить оплату в Railway finance_payments
    bd_pool = await get_bot_data_pool()
    async with bd_pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM public.finance_payments
               WHERE telegram_id = $1 AND code = ANY($2) AND success = TRUE""",
            telegram_id, seminar_codes,
        )
    return (count or 0) > 0


async def get_user_seminar_codes(telegram_id: int) -> set[str]:
    """Получить set кодов оплаченных семинаров пользователя."""
    # Шаг 1: все оплаченные codes пользователя из Railway
    bd_pool = await get_bot_data_pool()
    async with bd_pool.acquire() as conn:
        paid_rows = await conn.fetch(
            "SELECT DISTINCT code FROM public.finance_payments WHERE telegram_id = $1 AND success = TRUE",
            telegram_id,
        )
    if not paid_rows:
        return set()
    paid_codes = [r["code"] for r in paid_rows]

    # Шаг 2: фильтр — только коды, которые являются семинарами в Neon reference
    ref_pool = await get_reference_pool()
    async with ref_pool.acquire() as conn:
        seminar_rows = await conn.fetch(
            "SELECT code FROM public.product WHERE code = ANY($1) AND type = 'seminar'",
            paid_codes,
        )
    return {r["code"] for r in seminar_rows}
