from __future__ import annotations

"""
Доставка приглашения в чат потока после оплаты программы/резидентуры/
семинара через Aisystant (WP-5, покупка типа INTERNSHIP).

Контекст: create_internship_payment() (purpose=INTERNSHIP) отдаёт пользователю
ссылку на оплату у Aisystant, но, в отличие от SEMINAR/WORKSHOP, завершение
такой оплаты никогда не бьёт вебхуком в наш бот. На старом боте
(@SystemsSchool_bot) эта доставка сделана поллингом (threading.Timer,
check-payment); здесь тот же поллинг переносится на core.scheduler
(async cron) — регистрация платежа на отслеживание в этом модуле,
сама проверка и отправка сообщения — в core/scheduler.py.
"""

from db.connection import get_pool
from config import get_logger

logger = get_logger(__name__)

MAX_CHECK_ATTEMPTS = 30  # cron */2 мин → ~1 час поллинга до gave_up


async def create_pending_check(
    *,
    telegram_id: int,
    aisystant_id: str,
    payment_id: str,
    code: str,
    course_name: str,
    lang: str = "ru",
) -> int | None:
    """Поставить платёж на отслеживание. Идемпотентно по payment_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            """INSERT INTO public.internship_payment_checks
                   (telegram_id, aisystant_id, payment_id, code, course_name, lang)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (payment_id) DO NOTHING
               RETURNING id""",
            telegram_id, aisystant_id, payment_id, code, course_name, lang,
        )
    if row_id is None:
        logger.info(f"[InternshipPayments] duplicate payment_id={payment_id}, skipped")
    return row_id


async def create_internship_payment_tracked(
    *,
    chat_id: int,
    aisystant_id: str,
    code: str,
    amount: float,
    lang: str = "ru",
    course_name: str | None = None,
    payment_index: int | None = None,
) -> dict | None:
    """Создать платёж через Aisystant и поставить его на отслеживание доставки.

    Drop-in замена aisystant.create_internship_payment() везде, где после
    оплаты нужно прислать пользователю приглашение в чат потока.
    """
    from clients.aisystant import aisystant

    result = await aisystant.create_internship_payment(
        aisystant_id, code, amount, payment_index=payment_index,
    )
    if not result or not result.get("id"):
        return result

    try:
        await create_pending_check(
            telegram_id=chat_id,
            aisystant_id=aisystant_id,
            payment_id=str(result["id"]),
            code=code,
            course_name=course_name or code,
            lang=lang,
        )
    except Exception as e:
        logger.error(f"[InternshipPayments] tracking insert failed for payment_id={result.get('id')}: {e}")

    return result


async def get_pending_checks(limit: int = 50) -> list[dict]:
    """Платежи, ожидающие подтверждения (для scheduler-поллинга)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM public.internship_payment_checks
               WHERE status = 'pending' AND attempts < $1
               ORDER BY created_at
               LIMIT $2""",
            MAX_CHECK_ATTEMPTS, limit,
        )
    return [dict(r) for r in rows]


async def record_check_attempt(check_id: int, *, resolved_status: str | None = None) -> None:
    """Инкремент попытки; если resolved_status задан — финализировать запись.

    resolved_status: 'succeeded' | 'failed' | 'gave_up' | None (ещё pending).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if resolved_status is None:
            await conn.execute(
                """UPDATE public.internship_payment_checks
                   SET attempts = attempts + 1, checked_at = NOW()
                   WHERE id = $1""",
                check_id,
            )
        else:
            await conn.execute(
                """UPDATE public.internship_payment_checks
                   SET attempts = attempts + 1, checked_at = NOW(), status = $2
                   WHERE id = $1""",
                check_id, resolved_status,
            )
