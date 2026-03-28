"""
DB-запросы для Сообщества IWE: оплаты семинара + участники чатов (WP-181).

Таблицы: workshop_payments, community_members.
"""

from datetime import datetime, timedelta

from db.connection import get_pool
from config import get_logger

logger = get_logger(__name__)


# ── workshop_payments ──────────────────────────────────


async def create_workshop_payment(
    telegram_id: int,
    amount: float,
    *,
    source: str = "bot",
    aisystant_id: str | None = None,
    payment_id: str | None = None,
) -> int:
    """Создать запись об оплате семинара. Возвращает id записи."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            """INSERT INTO workshop_payments
                   (telegram_id, aisystant_id, amount, source, payment_id, status, created_at)
               VALUES ($1, $2, $3, $4, $5, 'pending', NOW())
               RETURNING id""",
            telegram_id, aisystant_id, amount, source, payment_id,
        )
    logger.info(f"[Workshop] payment created: id={row_id}, tg={telegram_id}, amount={amount}, source={source}")
    return row_id


async def confirm_workshop_payment(payment_id_or_row_id, *, by_payment_id: bool = False) -> bool:
    """Подтвердить оплату (status → success, paid_at → NOW)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if by_payment_id:
            result = await conn.execute(
                """UPDATE workshop_payments
                   SET status = 'success', paid_at = NOW()
                   WHERE payment_id = $1 AND status = 'pending'""",
                str(payment_id_or_row_id),
            )
        else:
            result = await conn.execute(
                """UPDATE workshop_payments
                   SET status = 'success', paid_at = NOW()
                   WHERE id = $1 AND status = 'pending'""",
                int(payment_id_or_row_id),
            )
    updated = result.split()[-1] != "0"
    logger.info(f"[Workshop] payment confirmed: {payment_id_or_row_id}, updated={updated}")
    return updated


async def create_and_confirm_payment(
    telegram_id: int,
    amount: float,
    *,
    source: str = "bot",
    aisystant_id: str | None = None,
    payment_id: str | None = None,
) -> int:
    """Создать и сразу подтвердить оплату (для webhook / прямой оплаты). Возвращает id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            """INSERT INTO workshop_payments
                   (telegram_id, aisystant_id, amount, source, payment_id, status, paid_at, created_at)
               VALUES ($1, $2, $3, $4, $5, 'success', NOW(), NOW())
               RETURNING id""",
            telegram_id, aisystant_id, amount, source, payment_id,
        )
    logger.info(f"[Workshop] payment created+confirmed: id={row_id}, tg={telegram_id}, source={source}")
    return row_id


async def get_workshop_payment_count(telegram_id: int) -> int:
    """Количество успешных оплат семинара по telegram_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM workshop_payments
               WHERE telegram_id = $1 AND status = 'success'""",
            telegram_id,
        )
    return count or 0


async def migrate_payments_to_aisystant(telegram_id: int, aisystant_id: str) -> int:
    """При склейке /link — привязать все оплаты к aisystant_id. Возвращает кол-во обновлённых."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE workshop_payments
               SET aisystant_id = $2
               WHERE telegram_id = $1 AND aisystant_id IS NULL""",
            telegram_id, aisystant_id,
        )
    count = int(result.split()[-1])
    if count > 0:
        logger.info(f"[Workshop] migrated {count} payments: tg={telegram_id} → aisystant={aisystant_id}")
    return count


# ── community_members ──────────────────────────────────


async def log_community_join(
    telegram_id: int,
    chat_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    source: str = "unknown",
):
    """Записать вступление в чат. Upsert: если уже есть active запись — пропускаем."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO community_members
                   (telegram_id, chat_id, username, first_name, source, joined_at)
               VALUES ($1, $2, $3, $4, $5, NOW())
               ON CONFLICT (telegram_id, chat_id) WHERE left_at IS NULL
               DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name""",
            telegram_id, chat_id, username, first_name, source,
        )
    logger.info(f"[Community] join: tg={telegram_id}, chat={chat_id}, source={source}")


async def log_community_leave(telegram_id: int, chat_id: int):
    """Записать выход из чата."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE community_members
               SET left_at = NOW()
               WHERE telegram_id = $1 AND chat_id = $2 AND left_at IS NULL""",
            telegram_id, chat_id,
        )
    logger.info(f"[Community] leave: tg={telegram_id}, chat={chat_id}")


async def get_community_stats(chat_id: int, *, days: int = 7) -> dict:
    """Статистика чата для /community_report.

    Возвращает: total, paid, free, new (list), left (list).
    """
    pool = await get_pool()
    since = datetime.utcnow() - timedelta(days=days)

    async with pool.acquire() as conn:
        # Всего активных
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM community_members WHERE chat_id = $1 AND left_at IS NULL",
            chat_id,
        )

        # По источнику
        source_rows = await conn.fetch(
            """SELECT source, COUNT(*) as cnt FROM community_members
               WHERE chat_id = $1 AND left_at IS NULL
               GROUP BY source""",
            chat_id,
        )
        source_map = {r["source"]: r["cnt"] for r in source_rows}

        # Новые за период
        new_members = await conn.fetch(
            """SELECT cm.telegram_id, cm.username, cm.first_name, cm.source, cm.joined_at,
                      wp.amount, wp.paid_at
               FROM community_members cm
               LEFT JOIN workshop_payments wp
                   ON cm.telegram_id = wp.telegram_id AND wp.status = 'success'
               WHERE cm.chat_id = $1 AND cm.joined_at >= $2
               ORDER BY cm.joined_at DESC""",
            chat_id, since,
        )

        # Вышедшие за период
        left_members = await conn.fetch(
            """SELECT telegram_id, username, first_name, left_at
               FROM community_members
               WHERE chat_id = $1 AND left_at >= $2
               ORDER BY left_at DESC""",
            chat_id, since,
        )

    return {
        "total": total or 0,
        "paid": source_map.get("payment", 0),
        "free": source_map.get("admin", 0) + source_map.get("unknown", 0),
        "new": [dict(r) for r in new_members],
        "left": [dict(r) for r in left_members],
    }
