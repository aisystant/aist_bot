"""
Управление подключением к базе данных.

Пул соединений PostgreSQL через asyncpg.
"""

import asyncpg
from typing import Optional

from config import DATABASE_URL, DT_DATABASE_URL, get_logger

logger = get_logger(__name__)

# Глобальный пул соединений (aist_bot БД)
_pool: Optional[asyncpg.Pool] = None

# Пул для digitaltwin БД (WP-227)
_dt_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """Получить пул соединений (создать если не существует)"""
    global _pool
    if _pool is None:
        try:
            _pool = await asyncpg.create_pool(
                DATABASE_URL,
                statement_cache_size=0,
                min_size=10,
                max_size=50,
                command_timeout=30,
            )
            logger.info("✅ Пул соединений создан (min=10, max=50)")
        except Exception as e:
            logger.error(f"❌ Ошибка создания пула соединений: {e}")
            raise
    return _pool


async def get_dt_pool() -> asyncpg.Pool:
    """Получить пул соединений к digitaltwin БД (WP-227).

    Если DT_DATABASE_URL не задан — использует DATABASE_URL (fallback до cutover).
    """
    global _dt_pool
    if _dt_pool is None:
        try:
            _dt_pool = await asyncpg.create_pool(
                DT_DATABASE_URL,
                statement_cache_size=0,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            logger.info("✅ DT пул соединений создан (digitaltwin)")
        except Exception as e:
            logger.error(f"❌ Ошибка создания DT пула соединений: {e}")
            raise
    return _dt_pool


async def close_pool():
    """Закрыть пул соединений"""
    global _pool, _dt_pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("🔒 Пул соединений закрыт")
    if _dt_pool:
        await _dt_pool.close()
        _dt_pool = None
        logger.info("🔒 DT пул соединений закрыт")


async def acquire():
    """Получить соединение из пула (для использования в async with)"""
    try:
        pool = await get_pool()
        return pool.acquire()
    except Exception as e:
        logger.error(f"❌ Ошибка получения соединения из пула: {e}")
        raise


# Для обратной совместимости
db_pool = None

async def init_db():
    """Инициализация базы данных (для обратной совместимости)"""
    global db_pool
    pool = await get_pool()
    db_pool = pool
    
    # Создание таблиц
    from .models import create_tables
    await create_tables(pool)
    
    logger.info("✅ База данных инициализирована")
    return pool
