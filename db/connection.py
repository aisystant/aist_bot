"""
Управление подключением к базе данных.

Пул соединений PostgreSQL через asyncpg.
"""

import asyncpg
from typing import Optional

from config import (
    DATABASE_URL,
    DT_DATABASE_URL,
    SUBSCRIPTION_DB_URL,
    PERSONA_URL,
    SUBSCRIPTION_URL,
    INDICATORS_URL,
    LEARNING_URL,
    get_logger,
)

logger = get_logger(__name__)

# Глобальный пул соединений (aist_bot БД)
_pool: Optional[asyncpg.Pool] = None

# Пул для digitaltwin БД (WP-227, DROPPED 26 апр — keep for transition migration)
_dt_pool: Optional[asyncpg.Pool] = None

# Пул для platform БД — subscription_grants, user_identities (WP-232, DROPPED 26 апр — keep for transition)
_platform_pool: Optional[asyncpg.Pool] = None

# WP-269 read-path migration: новые per-domain pools.
_persona_pool: Optional[asyncpg.Pool] = None       # persona.ory_identity, persona.identity_map
_subscription_pool: Optional[asyncpg.Pool] = None  # subscription.contract
_indicators_pool: Optional[asyncpg.Pool] = None    # indicators.calculated_profile (заменяет digital_twins)
_learning_pool: Optional[asyncpg.Pool] = None      # learning.domain_event (qa, notifications, traces)


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


async def get_platform_pool() -> asyncpg.Pool:
    """Получить пул соединений к platform БД (WP-232).

    Используется для subscription_grants, user_identities.
    Fallback: DT_DATABASE_URL → DATABASE_URL (до полного cutover WP-232).
    """
    global _platform_pool
    if _platform_pool is None:
        try:
            _platform_pool = await asyncpg.create_pool(
                SUBSCRIPTION_DB_URL,
                statement_cache_size=0,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )
            logger.info("✅ Platform пул соединений создан (subscription_grants)")
        except Exception as e:
            logger.error(f"❌ Ошибка создания platform пула соединений: {e}")
            raise
    return _platform_pool


async def get_persona_pool() -> asyncpg.Pool:
    """Пул соединений к persona БД (WP-269): ory_identity, identity_map."""
    global _persona_pool
    if _persona_pool is None:
        _persona_pool = await asyncpg.create_pool(
            PERSONA_URL,
            statement_cache_size=0,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
        logger.info("✅ Persona пул соединений создан")
    return _persona_pool


async def get_subscription_pool() -> asyncpg.Pool:
    """Пул соединений к subscription БД (WP-269): contract."""
    global _subscription_pool
    if _subscription_pool is None:
        _subscription_pool = await asyncpg.create_pool(
            SUBSCRIPTION_URL,
            statement_cache_size=0,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )
        logger.info("✅ Subscription пул соединений создан")
    return _subscription_pool


async def get_indicators_pool() -> asyncpg.Pool:
    """Пул соединений к indicators БД (WP-269): calculated_profile (заменяет digital_twins)."""
    global _indicators_pool
    if _indicators_pool is None:
        _indicators_pool = await asyncpg.create_pool(
            INDICATORS_URL,
            statement_cache_size=0,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )
        logger.info("✅ Indicators пул соединений создан")
    return _indicators_pool


async def get_learning_pool() -> asyncpg.Pool:
    """Пул соединений к learning БД (WP-269): domain_event (qa, notifications, traces)."""
    global _learning_pool
    if _learning_pool is None:
        _learning_pool = await asyncpg.create_pool(
            LEARNING_URL,
            statement_cache_size=0,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
        logger.info("✅ Learning пул соединений создан")
    return _learning_pool


async def close_pool():
    """Закрыть пул соединений"""
    global _pool, _dt_pool, _platform_pool, _persona_pool, _subscription_pool, _indicators_pool, _learning_pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("🔒 Пул соединений закрыт")
    if _dt_pool:
        await _dt_pool.close()
        _dt_pool = None
        logger.info("🔒 DT пул соединений закрыт")
    if _platform_pool:
        await _platform_pool.close()
        _platform_pool = None
        logger.info("🔒 Platform пул соединений закрыт")
    if _persona_pool:
        await _persona_pool.close()
        _persona_pool = None
        logger.info("🔒 Persona пул соединений закрыт")
    if _subscription_pool:
        await _subscription_pool.close()
        _subscription_pool = None
        logger.info("🔒 Subscription пул соединений закрыт")
    if _indicators_pool:
        await _indicators_pool.close()
        _indicators_pool = None
        logger.info("🔒 Indicators пул соединений закрыт")
    if _learning_pool:
        await _learning_pool.close()
        _learning_pool = None
        logger.info("🔒 Learning пул соединений закрыт")


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
