"""
DB-запросы для витрины семинаров (WP-5).

Таблица: products (type='seminar'), finance_payments.
Миграция: payment-registry/migrations/009-products-and-mentor-assignments.sql
"""

from db.connection import get_platform_pool
from config import get_logger

logger = get_logger(__name__)


# ── products (каталог семинаров) ─────────────────────


async def get_active_seminars(*, free_only: bool = False) -> list[dict]:
    """Получить активные семинары из products, отсортированные по sort_order."""
    pool = await get_platform_pool()
    async with pool.acquire() as conn:
        if free_only:
            rows = await conn.fetch(
                """SELECT * FROM products
                   WHERE type = 'seminar' AND active = TRUE AND is_free = TRUE
                   ORDER BY sort_order""",
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM products
                   WHERE type = 'seminar' AND active = TRUE
                   ORDER BY sort_order""",
            )
    return [dict(r) for r in rows]


async def get_seminar_by_code(code: str) -> dict | None:
    """Получить семинар по code (PK)."""
    pool = await get_platform_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM products WHERE code = $1 AND type = 'seminar' AND active = TRUE",
            code,
        )
    return dict(row) if row else None


async def get_seminar_by_tilda_uid(tilda_uid: str) -> dict | None:
    """Получить семинар по tilda_uid (в metadata)."""
    pool = await get_platform_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM products
               WHERE type = 'seminar' AND active = TRUE
                 AND metadata->>'tilda_uid' = $1""",
            tilda_uid,
        )
    return dict(row) if row else None


# ── finance_payments (оплата) ────────────────────────


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
    pool = await get_platform_pool()
    # Определяем channel: 9=bot-stars, 10=bot-yookassa
    channel = 9 if currency == "XTR" else 10

    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            """INSERT INTO finance_payments
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
    pool = await get_platform_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM finance_payments
               WHERE telegram_id = $1 AND code = $2 AND success = TRUE""",
            telegram_id, product_code,
        )
    return (count or 0) > 0


async def has_access_to_chat(telegram_id: int, chat_id: int) -> bool:
    """Проверить, есть ли у пользователя оплаченный семинар с данным chat_id."""
    pool = await get_platform_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM finance_payments fp
               JOIN products p ON p.code = fp.code
               WHERE fp.telegram_id = $1 AND p.tg_chat_id = $2
                 AND fp.success = TRUE AND p.type = 'seminar'""",
            telegram_id, chat_id,
        )
    return (count or 0) > 0


async def get_user_seminar_codes(telegram_id: int) -> set[str]:
    """Получить set кодов оплаченных семинаров пользователя."""
    pool = await get_platform_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT fp.code FROM finance_payments fp
               JOIN products p ON p.code = fp.code
               WHERE fp.telegram_id = $1 AND fp.success = TRUE AND p.type = 'seminar'""",
            telegram_id,
        )
    return {r["code"] for r in rows}
