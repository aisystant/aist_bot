"""
DB-запросы для витрины семинаров (WP-5).

Таблицы: seminars, seminar_payments.
"""

from db.connection import get_pool
from config import get_logger

logger = get_logger(__name__)


# ── seminars (каталог) ────────────────────────────────


async def get_active_seminars(*, free_only: bool = False) -> list[dict]:
    """Получить активные семинары, отсортированные по sort_order."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if free_only:
            rows = await conn.fetch(
                """SELECT * FROM seminars
                   WHERE active = TRUE AND is_free = TRUE
                   ORDER BY sort_order""",
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM seminars
                   WHERE active = TRUE
                   ORDER BY sort_order""",
            )
    return [dict(r) for r in rows]


async def get_seminar_by_id(seminar_id: int) -> dict | None:
    """Получить семинар по id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM seminars WHERE id = $1 AND active = TRUE",
            seminar_id,
        )
    return dict(row) if row else None


async def get_seminar_by_code(tilda_uid: str) -> dict | None:
    """Получить семинар по коду (tilda_uid)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM seminars WHERE tilda_uid = $1 AND active = TRUE",
            tilda_uid,
        )
    return dict(row) if row else None


# ── seminar_payments ──────────────────────────────────


async def create_seminar_payment(
    telegram_id: int,
    seminar_id: int,
    amount: float,
    currency: str = "RUB",
    *,
    source: str = "bot",
    payment_id: str | None = None,
) -> int:
    """Создать и сразу подтвердить оплату семинара. Возвращает id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            """INSERT INTO seminar_payments
                   (telegram_id, seminar_id, amount, currency, source, payment_id,
                    status, paid_at, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, 'success', NOW(), NOW())
               ON CONFLICT (payment_id) WHERE payment_id IS NOT NULL DO NOTHING
               RETURNING id""",
            telegram_id, seminar_id, amount, currency, source, payment_id,
        )
    if row_id is None:
        logger.info(f"[Showcase] duplicate payment_id={payment_id}, skipped")
        return 0
    logger.info(f"[Showcase] payment: id={row_id}, tg={telegram_id}, seminar={seminar_id}, amount={amount} {currency}")
    return row_id


async def has_seminar_access(telegram_id: int, seminar_id: int) -> bool:
    """Проверить, оплачен ли семинар пользователем."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM seminar_payments
               WHERE telegram_id = $1 AND seminar_id = $2 AND status = 'success'""",
            telegram_id, seminar_id,
        )
    return (count or 0) > 0


async def increment_seminar_views(seminar_id: int) -> int:
    """Инкрементировать счётчик просмотров. Возвращает новое значение."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """UPDATE seminars SET view_count = COALESCE(view_count, 0) + 1
               WHERE id = $1 RETURNING view_count""",
            seminar_id,
        )
    return count or 0


async def get_user_seminar_ids(telegram_id: int) -> set[int]:
    """Получить set id оплаченных семинаров пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT seminar_id FROM seminar_payments
               WHERE telegram_id = $1 AND status = 'success'""",
            telegram_id,
        )
    return {r["seminar_id"] for r in rows}
