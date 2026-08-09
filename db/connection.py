"""
Управление подключением к базе данных.

Пул соединений PostgreSQL через asyncpg.
"""

import asyncpg
from typing import Optional

from config import (
    DATABASE_URL,
    BOT_DATA_URL,
    PERSONA_URL,
    SUBSCRIPTION_URL,
    INDICATORS_URL,
    LEARNING_URL,
    REWARDS_URL,
    CONSENT_URL,
    FSM_URL,
    JOURNAL_URL,
    HEALTH_URL,
    SECRETS_URL,
    PUBLICATION_URL,
    COMMUNITY_URL,
    LEAD_URL,
    REFERENCE_URL,
    get_logger,
)

logger = get_logger(__name__)

# Глобальный пул соединений (Railway-local Postgres `bot_data` после WP-268 Phase 4
# lift-and-shift, 29 апр 2026): users, digital_twins, subscription_grants, products,
# finance_payments, marathon_content, reminders, user_state, user_events, и т.д.
# Tech debt: до полной 12-BC миграции (G1-G9, ≥W19) — см. DP.ARCH.004 §10.11.
_pool: Optional[asyncpg.Pool] = None

# WP-269 read-path migration: новые per-domain pools.
_persona_pool: Optional[asyncpg.Pool] = None       # persona.ory_identity, persona.identity_map
_subscription_pool: Optional[asyncpg.Pool] = None  # subscription.contract
_indicators_pool: Optional[asyncpg.Pool] = None    # indicators.calculated_profile (Память.Derived: ЦД)
_learning_pool: Optional[asyncpg.Pool] = None      # learning.domain_event (qa, notifications, traces)
_rewards_pool: Optional[asyncpg.Pool] = None       # rewards.point_balances (WP-253 Ф9.3 проекция)

# WP-188 Ф17: writer-pool для learning.tracking_consent через роль consent_writer (миграция 113).
# Отдельный pool — write-граница для GDPR. Размер маленький — операция редкая (онбординг + ручной /consent).
_consent_pool: Optional[asyncpg.Pool] = None       # learning.tracking_consent (consent_writer role)

# WP-268 Phase 3 Block 1: aiogram fsm_states вынесен в Railway-local Postgres (паттерн DP.ARCH.004 §10.10).
_fsm_pool: Optional[asyncpg.Pool] = None           # fsm_states (Railway-local Postgres)

# WP-268 Phase 3 Block 2: qa_history + feedback_triage вынесены в Neon journal БД (DP.ARCH.004 §3.2).
_journal_pool: Optional[asyncpg.Pool] = None       # qa_history, feedback_triage (Neon journal БД)

# WP-268 Phase 5 G5 Tier2: error_logs, user_sessions, pending_fixes → Neon health БД (DP.ARCH.004 §8).
_health_pool: Optional[asyncpg.Pool] = None        # error_logs, user_sessions, pending_fixes (Neon health БД)

# WP-253 Пробел C: OAuth-токены интеграций (GitHub и будущие) — Neon secrets БД.
# DP.ARCH.004 §B7.3.1: secrets ∩ PII → pgcrypto column-level + RLS.
_secrets_pool: Optional[asyncpg.Pool] = None       # github_connections (Neon secrets БД)

# WP-253 lift-and-shift (8 мая 2026): остальные BC БД для bot_data таблиц.
_publication_pool: Optional[asyncpg.Pool] = None   # scheduled_post, published_post, channel_monitor, channel_mention_log
_community_pool: Optional[asyncpg.Pool] = None     # club_account (discourse)
_lead_pool: Optional[asyncpg.Pool] = None          # conversion_event
_reference_pool: Optional[asyncpg.Pool] = None     # product, training_setting, training_child

# WP-253 tech debt: Railway /bot_data bridge для products + finance_payments до ETL в Neon.
# TODO: после ETL products → reference и finance_payments → payment — удалить этот pool (G3/G5).
_bot_data_pool: Optional[asyncpg.Pool] = None      # products, finance_payments (Railway /bot_data)


async def get_pool() -> asyncpg.Pool:
    """Получить пул соединений (создать если не существует)"""
    global _pool
    if _pool is None:
        try:
            _pool = await asyncpg.create_pool(
                DATABASE_URL,
                statement_cache_size=100,
                min_size=10,
                max_size=50,
                command_timeout=30,
                max_inactive_connection_lifetime=60,
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
            statement_cache_size=100,
            min_size=2,  # WP-330 латентность: держать соединения тёплыми (nav hot-path: resolve_ory)
            max_size=10,
            command_timeout=30,
            max_inactive_connection_lifetime=300,  # WP-330: было 60 → реконнект на каждой nav-команде
        )
        logger.info("✅ Persona пул соединений создан")
    return _persona_pool


async def get_subscription_pool() -> asyncpg.Pool:
    """Пул соединений к subscription БД (WP-269): contract."""
    global _subscription_pool
    if _subscription_pool is None:
        _subscription_pool = await asyncpg.create_pool(
            SUBSCRIPTION_URL,
            statement_cache_size=100,
            min_size=1,
            max_size=5,
            command_timeout=30,
            max_inactive_connection_lifetime=60,
        )
        logger.info("✅ Subscription пул соединений создан")
    return _subscription_pool


async def get_indicators_pool() -> asyncpg.Pool:
    """Пул соединений к indicators БД (WP-269): calculated_profile (заменяет digital_twins)."""
    global _indicators_pool
    if _indicators_pool is None:
        _indicators_pool = await asyncpg.create_pool(
            INDICATORS_URL,
            statement_cache_size=100,
            min_size=1,
            max_size=5,
            command_timeout=30,
            max_inactive_connection_lifetime=60,
        )
        logger.info("✅ Indicators пул соединений создан")
    return _indicators_pool


async def get_learning_pool() -> asyncpg.Pool:
    """Пул соединений к learning БД (WP-269): domain_event (qa, notifications, traces)."""
    global _learning_pool
    if _learning_pool is None:
        _learning_pool = await asyncpg.create_pool(
            LEARNING_URL,
            statement_cache_size=0,  # Neon pooled endpoint: prepared stmt cache вызывает UndefinedColumnError при переключении соединений
            min_size=2,  # WP-330 латентность: nav hot-path (/progress feed/answers)
            max_size=10,
            command_timeout=30,
            max_inactive_connection_lifetime=300,  # WP-330: было 60
        )
        logger.info("✅ Learning пул соединений создан")
    return _learning_pool


async def get_rewards_pool() -> asyncpg.Pool:
    """Пул соединений к rewards БД (WP-253 Ф9.3): point_balances.

    Read-only для бота — writer = projection-worker (DP.SC.122). Latency p95 ≤1s
    после INSERT в learning.domain_event.
    """
    global _rewards_pool
    if _rewards_pool is None:
        _rewards_pool = await asyncpg.create_pool(
            REWARDS_URL,
            statement_cache_size=100,
            min_size=1,
            max_size=5,
            command_timeout=30,
            max_inactive_connection_lifetime=60,
        )
        logger.info("✅ Rewards пул соединений создан")
    return _rewards_pool


async def get_consent_pool() -> asyncpg.Pool:
    """Пул соединений writer-pool для learning.tracking_consent (WP-188 Ф17).

    Использует роль `consent_writer` (миграция 113): INSERT/UPDATE/DELETE/SELECT
    только на tracking_consent. BYPASSRLS — caller ОБЯЗАН передавать явный
    `WHERE account_id = $1` (L2-PRIVACY, см. lessons_privacy_layer2_required.md).

    Маленький pool: операция редкая (онбординг + ручной /consent).
    """
    global _consent_pool
    if _consent_pool is None:
        _consent_pool = await asyncpg.create_pool(
            CONSENT_URL,
            statement_cache_size=0,  # Neon pooled endpoint compatibility
            min_size=2,  # WP-330 латентность: nav hot-path (/settings typing-tracking)
            max_size=3,
            command_timeout=15,
            max_inactive_connection_lifetime=300,  # WP-330: было 60
        )
        logger.info("✅ Consent пул соединений создан")
    return _consent_pool


async def get_fsm_pool() -> asyncpg.Pool:
    """Пул соединений к fsm БД (WP-268 Phase 3 Block 1, паттерн DP.ARCH.004 §10.10): fsm_states.

    Railway-local Postgres рядом с ботом. State-files живут вне Neon entity-БД
    по принципу различения «State file ≠ Лог ≠ Инцидент» (DP.D.049).
    """
    global _fsm_pool
    if _fsm_pool is None:
        _fsm_pool = await asyncpg.create_pool(
            FSM_URL,
            statement_cache_size=100,
            min_size=2,
            max_size=20,
            command_timeout=30,
            max_inactive_connection_lifetime=60,
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
            statement_cache_size=100,
            min_size=1,
            max_size=10,
            command_timeout=30,
            max_inactive_connection_lifetime=60,
        )
        logger.info("✅ Journal пул соединений создан (min=1, max=10)")
    return _journal_pool


async def get_health_pool() -> asyncpg.Pool:
    """Пул соединений к health БД (WP-268 Phase 5 G5 Tier2): error_logs, user_sessions, pending_fixes.

    Neon БД `health` (DP.ARCH.004 §8) — наблюдаемость системы и сессии пользователей.
    """
    global _health_pool
    if _health_pool is None:
        _health_pool = await asyncpg.create_pool(
            HEALTH_URL,
            statement_cache_size=100,
            min_size=2,  # WP-330: nav-red early indicator — keep connections warm
            max_size=10,
            command_timeout=30,
            max_inactive_connection_lifetime=300,  # WP-330: было 60 → reconnect on every nav burst
        )
        logger.info("✅ Health пул соединений создан (min=1, max=10)")
    return _health_pool


async def get_secrets_pool() -> asyncpg.Pool:
    """Пул соединений к secrets БД (WP-253 Пробел C): github_connections.

    Neon БД `secrets` (DP.ARCH.004 §B7.3.1) — OAuth-токены интеграций.
    Токены хранятся в зашифрованном виде (pgp_sym_encrypt + RLS).
    """
    global _secrets_pool
    if _secrets_pool is None:
        _secrets_pool = await asyncpg.create_pool(
            SECRETS_URL,
            statement_cache_size=0,  # Neon pooled endpoint: prepared stmt cache вызывает UndefinedTableError (search_path сбрасывается на переиспользуемых pgbouncer-соединениях)
            min_size=1,
            max_size=5,
            command_timeout=30,
            max_inactive_connection_lifetime=60,
        )
        logger.info("✅ Secrets пул соединений создан (min=1, max=5)")
    return _secrets_pool


async def get_publication_pool() -> asyncpg.Pool:
    """Пул соединений к publication БД (WP-253 lift-and-shift): scheduled_post, published_post, channel_monitor, channel_mention_log."""
    global _publication_pool
    if _publication_pool is None:
        _publication_pool = await asyncpg.create_pool(
            PUBLICATION_URL, statement_cache_size=100, min_size=1, max_size=5, command_timeout=30, max_inactive_connection_lifetime=60,
        )
        logger.info("✅ Publication пул соединений создан")
    return _publication_pool


async def get_community_pool() -> asyncpg.Pool:
    """Пул соединений к community БД (WP-253 lift-and-shift): club_account (discourse), mentorship."""
    global _community_pool
    if _community_pool is None:
        _community_pool = await asyncpg.create_pool(
            COMMUNITY_URL, statement_cache_size=100, min_size=1, max_size=5, command_timeout=30, max_inactive_connection_lifetime=60,
        )
        logger.info("✅ Community пул соединений создан")
    return _community_pool


async def get_lead_pool() -> asyncpg.Pool:
    """Пул соединений к lead БД (WP-253 lift-and-shift): conversion_event, funnel_record, claim."""
    global _lead_pool
    if _lead_pool is None:
        _lead_pool = await asyncpg.create_pool(
            LEAD_URL, statement_cache_size=100, min_size=1, max_size=5, command_timeout=30, max_inactive_connection_lifetime=60,
        )
        logger.info("✅ Lead пул соединений создан")
    return _lead_pool


async def get_reference_pool() -> asyncpg.Pool:
    """Пул соединений к reference БД (WP-253 lift-and-shift): product, training_setting, training_child, tariffs."""
    global _reference_pool
    if _reference_pool is None:
        _reference_pool = await asyncpg.create_pool(
            REFERENCE_URL,
            statement_cache_size=0,  # Neon pooled endpoint: prepared stmt cache вызывает UndefinedTableError (search_path сбрасывается на переиспользуемых pgbouncer-соединениях)
            min_size=1, max_size=5, command_timeout=30, max_inactive_connection_lifetime=60,
        )
        logger.info("✅ Reference пул соединений создан")
    return _reference_pool


async def get_bot_data_pool() -> asyncpg.Pool:
    """Пул соединений к Railway /bot_data (tech debt bridge, WP-253).

    Содержит public.products + public.finance_payments до завершения ETL в Neon.
    TODO: удалить после G3 (finance_payments → payment) и G5 (products ETL → reference).
    """
    global _bot_data_pool
    if _bot_data_pool is None:
        _bot_data_pool = await asyncpg.create_pool(
            BOT_DATA_URL, statement_cache_size=100, min_size=1, max_size=5, command_timeout=30, max_inactive_connection_lifetime=60,
        )
        logger.info("✅ BotData пул соединений создан (tech debt bridge)")
    return _bot_data_pool


async def close_pool():
    """Закрыть пул соединений"""
    global _pool, _persona_pool, _subscription_pool, _indicators_pool, _learning_pool, _rewards_pool, _consent_pool, _fsm_pool, _journal_pool, _health_pool, _secrets_pool, _publication_pool, _community_pool, _lead_pool, _reference_pool, _bot_data_pool
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
    if _rewards_pool:
        await _rewards_pool.close()
        _rewards_pool = None
        logger.info("🔒 Rewards пул соединений закрыт")
    if _consent_pool:
        await _consent_pool.close()
        _consent_pool = None
        logger.info("🔒 Consent пул соединений закрыт")
    if _fsm_pool:
        await _fsm_pool.close()
        _fsm_pool = None
        logger.info("🔒 FSM пул соединений закрыт")
    if _journal_pool:
        await _journal_pool.close()
        _journal_pool = None
        logger.info("🔒 Journal пул соединений закрыт")
    if _health_pool:
        await _health_pool.close()
        _health_pool = None
        logger.info("🔒 Health пул соединений закрыт")
    if _secrets_pool:
        await _secrets_pool.close()
        _secrets_pool = None
        logger.info("🔒 Secrets пул соединений закрыт")
    if _publication_pool:
        await _publication_pool.close()
        _publication_pool = None
    if _community_pool:
        await _community_pool.close()
        _community_pool = None
    if _lead_pool:
        await _lead_pool.close()
        _lead_pool = None
    if _reference_pool:
        await _reference_pool.close()
        _reference_pool = None
    if _bot_data_pool:
        await _bot_data_pool.close()
        _bot_data_pool = None


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

async def _verify_schema(pool: asyncpg.Pool) -> None:
    """Проверка критичных таблиц в ТЕХ ЖЕ пулах, откуда они читаются в рантайме.

    Контракт «verify-пул == read-пул» (WP-330 A-zero, peer-session 2026-06-03-18):
    каждую таблицу проверяем в инстансе, куда РЕАЛЬНО идут запросы, а не в
    bot_data «по умолчанию». После 12-BC миграции (WP-268/269/253):
      - feed_sessions читается из learning-пула (db/queries/profile.py),
      - feedback_triage — из journal-пула (db/queries/profile.py:218 DELETE,
        core/feedback_triage.py INSERT/UPDATE).
    Старая версия проверяла обе в bot_data → ложная зелёнка на feed_sessions
    (есть в bot_data, нет в learning) и ложное падение на feedback_triage
    (есть в journal, нет в bot_data) → crash-loop деплоя 2026-06-03.

    НЕ-фатально: детектор дрейфа НЕ имеет права ронять прод. При дрейфе —
    ERROR-лог + TG-алерт, но без RuntimeError (иначе страж крэшит деплой,
    как 2026-06-03 07:51 UTC). feed_sessions/feedback_triage — некритичные
    (статистика /progress, admin-триаж); крэшить весь бот для 50 пользователей
    из-за них нельзя. Критичные пользовательские таблицы fail-fast'ятся на
    create_tables() выше.

    Параметр `pool` сохранён для обратной совместимости вызова (init_db), но
    проверки идут по правильным per-domain пулам.
    """
    missing: list[str] = []

    # 1. feed_sessions — learning-пул (read path: profile.py)
    try:
        lpool = await get_learning_pool()
        async with lpool.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass('public.feed_sessions')"):
                missing.append("feed_sessions@learning")
    except Exception as e:
        logger.warning(f"[schema-verify] feed_sessions@learning check skipped: {e}")

    # 2. feedback_triage.suggested_action — journal-пул (read path: feedback.py)
    try:
        jpool = await get_journal_pool()
        async with jpool.acquire() as conn:
            col_exists = await conn.fetchval(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'feedback_triage'
                  AND column_name = 'suggested_action'
                """
            )
            if not col_exists:
                missing.append("feedback_triage.suggested_action@journal")
    except Exception as e:
        logger.warning(f"[schema-verify] feedback_triage@journal check skipped: {e}")

    # 3. learning.consent_grant — learning-пул (write path: db/queries/consent.py).
    # peer-session 2026-06-05-02: эта таблица отсутствовала на pilot и каждый тап
    # кнопки согласия падал db/L4, а guard был зелёным (consent_grant не было в
    # списке). Миграция 023 создаёт её; эта проверка не даёт guard'у снова замолчать.
    try:
        lpool2 = await get_learning_pool()
        async with lpool2.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass('learning.consent_grant')"):
                missing.append("learning.consent_grant@learning")
    except Exception as e:
        logger.warning(f"[schema-verify] consent_grant@learning check skipped: {e}")

    # 4. public.training_setting — reference-пул. REFERENCE_URL может указывать
    # на Neon pooled endpoint, где session search_path не является контрактом.
    # Явная схема здесь и в query-layer сохраняет проверку и runtime read-path
    # работоспособными даже при search_path=pg_catalog.
    try:
        rpool = await get_reference_pool()
        async with rpool.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass('public.training_setting')"):
                missing.append("public.training_setting@reference")
    except Exception as e:
        logger.warning(f"[schema-verify] training_setting@reference check skipped: {e}")

    if missing:
        msg = f"Schema drift detected: {', '.join(missing)} missing. Run migrations."
        logger.error(f"❌ {msg} (non-fatal — bot continues, see WP-330 A-zero)")
        # Fire-and-forget TG alert (best effort — bot not fully started yet)
        async def _alert():
            try:
                import os
                token = os.getenv("TELEGRAM_BOT_TOKEN")
                dev_chat = os.getenv("DEVELOPER_CHAT_ID")
                if token and dev_chat:
                    from aiogram import Bot
                    bot = Bot(token=token)
                    try:
                        await bot.send_message(
                            int(dev_chat),
                            f"🚨 <b>Schema drift</b> (non-fatal)\n<code>{msg}</code>",
                            parse_mode="HTML",
                        )
                    finally:
                        await bot.session.close()
            except Exception:
                pass
        # Await напрямую (а не detached task): init_db выполняется до start_polling,
        # detached task мог не успеть выполниться до рестарта → алерт терялся.
        # _alert полностью обёрнут в try/except → не может пробросить исключение.
        await _alert()
        # NON-FATAL: НЕ raise — страж не должен крэшить прод.
        return

    logger.info(
        "✅ Schema verify passed (learning.feed_sessions, journal.feedback_triage, "
        "learning.consent_grant, public.training_setting)"
    )


async def init_db():
    """Инициализация базы данных (для обратной совместимости)"""
    global db_pool
    pool = await get_pool()
    db_pool = pool

    # Создание таблиц
    from .models import create_tables
    await create_tables(pool)

    # Fail-fast: проверить что критичные таблицы на месте
    await _verify_schema(pool)

    logger.info("✅ База данных инициализирована")
    return pool
