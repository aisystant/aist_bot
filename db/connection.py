"""
Управление подключением к базе данных.

Пул соединений PostgreSQL через asyncpg.
"""

import asyncpg
from typing import Optional

from config import DATABASE_URL, get_logger

logger = get_logger(__name__)

# Глобальный пул соединений
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """Получить пул соединений (создать если не существует)"""
    global _pool
    if _pool is None:
        try:
            _pool = await asyncpg.create_pool(DATABASE_URL)
            logger.info("✅ Пул соединений создан")
        except Exception as e:
            logger.error(f"❌ Ошибка создания пула соединений: {e}")
            raise
    return _pool


async def close_pool():
    """Закрыть пул соединений"""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("🔒 Пул соединений закрыт")


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
