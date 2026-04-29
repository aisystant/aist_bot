"""
Управление подключением к базе данных.

Пул соединений PostgreSQL через asyncpg.
"""

import asyncpg
from typing import Optional

from config import (
    DATABASE_URL,
    PERSONA_URL,
    SUBSCRIPTION_URL,
    INDICATORS_URL,
    LEARNING_URL,
    FSM_URL,
    JOURNAL_URL,
    get_logger,
)

logger = get_logger(__name__)

# Глобальный пул соединений (platform БД — legacy таблицы: users, digital_twins,
# subscription_grants, products, finance_payments, fsm_states, и т.д.)
_pool: Optional[asyncpg.Pool] = None

# WP-269 read-path migration: новые per-domain pools.
_persona_pool: Optional[asyncpg.Pool] = None       # persona.ory_identity, persona.identity_map
_subscription_pool: Optional[asyncpg.Pool] = None  # subscription.contract
_indicators_pool: Optional[asyncpg.Pool] = None    # indicators.calculated_profile (заменяет digital_twins)
_learning_pool: Optional[asyncpg.Pool] = None      # learning.domain_event (qa, notifications, traces)

# WP-268 Phase 3 Block 1: aiogram fsm_states вынесен в Railway-local Postgres (паттерн DP.ARCH.004 §10.10).
_fsm_pool: Optional[asyncpg.Pool] = None           # fsm_states (Railway-local Postgres)

# WP-268 Phase 3 Block 2: qa_history + feedback_triage вынесены в Neon journal БД (DP.ARCH.004 §3.2).
_journal_pool: Optional[asyncpg.Pool] = None       # qa_history, feedback_triage (Neon journal БД)


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


async def get_fsm_pool() -> asyncpg.Pool:
    """Пул соединений к fsm БД (WP-268 Phase 3 Block 1, паттерн DP.ARCH.004 §10.10): fsm_states.

    Railway-local Postgres рядом с ботом. State-files живут вне Neon entity-БД
    по принципу различения «State file ≠ Лог ≠ Инцидент» (DP.D.049).
    """
    global _fsm_pool
    if _fsm_pool is None:
        _fsm_pool = await asyncpg.create_pool(
            FSM_URL,
            statement_cache_size=0,
            min_size=2,
            max_size=20,
            command_timeout=30,
        )
        logger.info("✅ FSM пул соединений создан (min=2, max=20)")
    return _fsm_pool


async def get_journal_pool() -> asyncpg.Pool:
    """Пул соединений к journal БД (WP-268 Phase 3 Block 2): qa_history, feedback_triage.

    Neon БД `journal` (DP.ARCH.004 §3.2) — Память.Observed: session events
    с PII content (Q&A текст). Категория WP-257.
    """
    global _journal_pool
    if _journal_pool is None:
        _journal_pool = await asyncpg.create_pool(
            JOURNAL_URL,
            statement_cache_size=0,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
        logger.info("✅ Journal пул соединений создан (min=1, max=10)")
    return _journal_pool


async def close_pool():
    """Закрыть пул соединений"""
    global _pool, _persona_pool, _subscription_pool, _indicators_pool, _learning_pool, _fsm_pool, _journal_pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("🔒 Пул соединений закрыт")
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
    if _fsm_pool:
        await _fsm_pool.close()
        _fsm_pool = None
        logger.info("🔒 FSM пул соединений закрыт")
    if _journal_pool:
        await _journal_pool.close()
        _journal_pool = None
        logger.info("🔒 Journal пул соединений закрыт")


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
