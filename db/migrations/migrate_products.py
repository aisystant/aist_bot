"""
One-time migration: products from Railway /bot_data → Neon reference.product.

WP-253 G5: вызывается при старте бота, если reference.product пуста.
После успешного переноса showcase.py читает из Neon reference.
finance_payments остаётся в Railway /bot_data до G3.
"""

import asyncpg
import logging

from db.sql_helpers import insert_into as _insert_into_sql, select_from as _select_from_sql

logger = logging.getLogger(__name__)

_COMMON_COLS = (
    "code", "type", "title", "description", "format",
    "price_rub", "price_stars", "currency", "is_free", "installment_ok",
    "speaker", "tg_chat_id", "video_url", "event_date", "duration",
    "active", "show_in_catalog", "sort_order",
)


async def migrate_products_if_needed(
    bot_data_pool: asyncpg.Pool,
    reference_pool: asyncpg.Pool,
) -> int:
    """Копирует products из Railway /bot_data → Neon reference.product.

    Idempotent: пропускает если reference.product уже непуста.
    Возвращает количество скопированных строк (0 = уже было мигрировано).
    """
    async with reference_pool.acquire() as ref_conn:
        existing = await ref_conn.fetchval("SELECT COUNT(*) FROM product")
        if existing > 0:
            logger.info(f"[migrate_products] reference.product уже содержит {existing} строк, пропуск")
            return 0

    logger.info("[migrate_products] reference.product пуста — начинаем копирование из /bot_data.products")

    async with bot_data_pool.acquire() as src_conn:
        # Определить доступные колонки в Railway products
        available = {
            r["column_name"]
            for r in await src_conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'products'"
            )
        }
        logger.info(f"[migrate_products] Railway products columns: {sorted(available)}")

        # Собираем SELECT только из имеющихся колонок
        select_parts = [c for c in _COMMON_COLS if c in available]

        # metadata: либо из колонки, либо строим из tilda_uid
        if "metadata" in available:
            select_parts.append("COALESCE(metadata, '{}') AS metadata")
        elif "tilda_uid" in available:
            select_parts.append(
                "CASE WHEN tilda_uid IS NOT NULL "
                "THEN jsonb_build_object('tilda_uid', tilda_uid) "
                "ELSE '{}'::jsonb END AS metadata"
            )
        else:
            select_parts.append("'{}'::jsonb AS metadata")

        rows = await src_conn.fetch(_select_from_sql('products', select_parts))

    if not rows:
        logger.warning("[migrate_products] Railway products пуст — нечего копировать")
        return 0

    # Определить какие колонки реально попали в результат
    result_cols = list(rows[0].keys())
    placeholders = ", ".join(f"${i + 1}" for i in range(len(result_cols)))

    async with reference_pool.acquire() as ref_conn:
        async with ref_conn.transaction():
            copied = 0
            for row in rows:
                values = [row[c] for c in result_cols]
                query = _insert_into_sql(
                    'product',
                    result_cols,
                    placeholders,
                    'source',
                    "ON CONFLICT (code) DO NOTHING",
                )
                await ref_conn.execute(query, *values)
                copied += 1

    logger.info(f"[migrate_products] ✅ Скопировано {copied} строк в reference.product")
    return copied
