"""
AI System Track (@aist_track_bot) — Telegram-бот для системного развития
GitHub: https://github.com/aisystant/aist_track_bot

Миссия: Помочь стажёрам трансформироваться из людей с «непродуктивными убеждениями»
и случайных учеников в систематических учеников, которые собраны и удерживают
внимание на своём системном развитии.

С поддержкой PostgreSQL для хранения данных пользователей.
"""

import asyncio
import logging
import os
import signal
import sys
import warnings

# Подавить Pydantic warning из aiogram (model_custom_emoji_id protected namespace)
warnings.filterwarnings("ignore", message=".*model_custom_emoji_id.*protected namespace.*")

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat

# Feature flags
from config import USE_STATE_MACHINE, MULTILANG_ENABLED

# Импорты из модульных компонентов
from clients.claude import ClaudeClient
from db import init_db
from db.queries import get_intern, update_intern, get_topics_today
from integrations.telegram.keyboards import kb_update_profile, progress_bar

# ============= КОНФИГУРАЦИЯ =============

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
KNOWLEDGE_MCP_URL = os.getenv("KNOWLEDGE_MCP_URL", "https://knowledge-mcp.aisystant.workers.dev/mcp")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не установлен!")
if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY не установлен!")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не установлен!")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
# Rule 10.18: Suppress scheduler heartbeat noise (Running/executed ~4 lines/min)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ============= КОНСТАНТЫ (из config) =============
from config import (
    DIFFICULTY_LEVELS, LEARNING_STYLES, EXPERIENCE_LEVELS,
    STUDY_DURATIONS, BLOOM_LEVELS, BLOOM_AUTO_UPGRADE_AFTER,
    DAILY_TOPICS_LIMIT, MAX_TOPICS_PER_DAY, MARATHON_DAYS,
    ONTOLOGY_RULES,
)

# ============= ДОМЕННАЯ ЛОГИКА (из core/topics) =============
from core.topics import (
    load_topic_metadata, get_bloom_questions, get_search_keys,
    load_knowledge_structure, TOPICS, MARATHON_META,
    get_topic, get_topic_title, get_total_topics, get_marathon_day,
    get_topics_for_day, get_available_topics, get_sections_progress,
    get_lessons_tasks_progress, get_days_progress, score_topic_by_interests,
    get_next_topic_index, get_practice_for_day, has_pending_practice,
    get_theory_for_day, has_pending_theory, was_theory_sent_today,
    EXAMPLE_TEMPLATES, EXAMPLE_SOURCES, get_example_rules, get_personalization_prompt,
    save_answer,
)

# ============= ИНФРАСТРУКТУРА (из core/) =============
from core.storage import PostgresStorage
from core.middleware import MaintenanceMiddleware, LoggingMiddleware, ConsultationPassthroughMiddleware, TracingMiddleware, RateLimitMiddleware, UpdateDedupMiddleware

# ============= СОСТОЯНИЯ FSM (re-exports для обратной совместимости) =============
from handlers.onboarding import OnboardingStates
from handlers.legacy.learning import LearningStates
from handlers.legacy.learning import (
    send_topic, send_theory_topic, send_practice_topic,
    on_answer, on_work_product, on_bonus_answer,
)
from handlers.settings import UpdateStates, _show_update_screen
from handlers.progress import cmd_progress
from handlers.legacy.fallback_handler import legacy_on_unknown_message as _legacy_on_unknown_message

# ============= CLAUDE API =============
claude = ClaudeClient()

# State Machine (инициализируется в main() если USE_STATE_MACHINE=true)
state_machine = None

# ============= ЗАПУСК =============

async def _validate_middleware():
    """Boot-time: проверить что все middleware импортируются без ошибок.

    Если lazy import в __call__ сломан — краш здесь, ДО регистрации webhook.
    Railway не заменит рабочий инстанс сломанным.
    """
    from core.middleware import (
        RateLimitMiddleware,
        MaintenanceMiddleware,
        LoggingMiddleware,
        TracingMiddleware,
        ConsultationPassthroughMiddleware,
        UpdateDedupMiddleware,
    )
    from config.settings import DEVELOPER_CHAT_ID, MAINTENANCE_MODE, ALLOWED_TESTERS, MAINTENANCE_REDIRECT_BOT
    logger.info("✅ Middleware validation passed")


async def _bootstrap_learning_schema() -> None:
    """Migrações de learning-pool rodadas em background após o web server subir.

    Moved out of init_db() so Railway healthcheck is not blocked by the
    first-run table creation (migration 025 creates ~15 tables on first deploy).
    """
    import importlib as _il
    from db.connection import get_learning_pool

    # Canary state + restore pause across redeploys
    try:
        _m016 = _il.import_module("db.migrations.016_canary_state")
        _lpool = await get_learning_pool()
        _created = await _m016.migrate_if_needed(_lpool)
        if _created:
            logger.info("✅ Migration 016: canary_state created in learning DB")
        from clients.claude import set_canary_pool, restore_canary_from_db
        set_canary_pool(_lpool)
        await restore_canary_from_db(_lpool)
    except Exception as _e:
        logger.warning(f"⚠️ Canary state init skipped: {_e}")

    # Full learning schema for Railway Postgres pilot (WP-7 Ф-Pilot-LearningDB-Isolation)
    try:
        _m025 = _il.import_module("db.migrations.025_learning_schema_railway")
        _lpool = await get_learning_pool()
        _created = await _m025.migrate_if_needed(_lpool)
        if _created:
            logger.info("✅ Migration 025: learning schema bootstrapped in learning DB")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 025 skipped: {_e}")

    # consent_grant table
    try:
        _m023 = _il.import_module("db.migrations.023_consent_grant")
        _lpool = await get_learning_pool()
        _created = await _m023.migrate_if_needed(_lpool)
        if _created:
            logger.info("✅ Migration 023: consent_grant created in learning DB")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 023 skipped: {_e}")

    # reminder.bot_id NOT NULL
    try:
        _m024 = _il.import_module("db.migrations.024_reminder_bot_id_not_null")
        _lpool = await get_learning_pool()
        await _m024.migrate_if_needed(_lpool)
        logger.info("✅ Migration 024: reminder.bot_id NOT NULL ensured (learning DB)")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 024 skipped: {_e}")

    # WP-427 Ф6.1: learning.homework_content — LMS homework answer texts (consent-gated).
    try:
        _m033 = _il.import_module("db.migrations.033_homework_content")
        _lpool = await get_learning_pool()
        if await _m033.migrate_if_needed(_lpool):
            logger.info("✅ Migration 033: homework_content created in learning DB")
        else:
            logger.info("✅ Migration 033: homework_content already exists")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 033 (homework_content) skipped: {_e}")

    # Fix stuck marathon_progress after IndeterminateDatatypeError in update_progress
    # (bug fixed 2026-06-29, commit 72df544). Idempotent — no-op when no stuck users.
    try:
        _m036 = _il.import_module("db.migrations.036_fix_stuck_marathon_progress")
        _lpool = await get_learning_pool()
        if await _m036.migrate_if_needed(_lpool):
            logger.info("✅ Migration 036: stuck marathon_progress fixed")
        else:
            logger.info("✅ Migration 036: no stuck marathon users")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 036 skipped: {_e}")


async def main():
    global state_machine

    # Boot-time валидация middleware (ловит ImportError до webhook)
    await _validate_middleware()

    # Инициализация БД
    await init_db()

    # Миграция 031: отметки Онбордера (Х2/Х3) в development.user_state.
    # В проде create_tables пропущена флагом SKIP_DB_MIGRATIONS, поэтому колонки
    # добавляем явным вызовом миграции — как learning-миграции в _bootstrap_learning_schema.
    # Без этого should_offer падает на отсутствующей колонке и кнопка «Освоиться» молчит.
    try:
        import importlib as _il
        from db.connection import get_pool as _get_pool
        _m031 = _il.import_module("db.migrations.031_onboarder_completion_marks")
        if await _m031.migrate_if_needed(await _get_pool()):
            logger.info("✅ Migration 031: онбордер-отметки добавлены в development.user_state")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 031 (онбордер-отметки) skipped: {_e}", exc_info=True)

    # Миграция 032: notification_queue — очередь Доставщика (WP-418).
    # create_tables пропущена в проде (SKIP_DB_MIGRATIONS), поэтому явный вызов.
    try:
        _m032 = _il.import_module("db.migrations.032_notification_queue")
        if await _m032.migrate_if_needed(await _get_pool()):
            logger.info("✅ Migration 032: notification_queue создана")
        else:
            logger.info("✅ Migration 032: notification_queue уже существует")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 032 (notification_queue) skipped: {_e}", exc_info=True)

    # Миграция 037: scheduled_post — дедупликация + atomic publish lock (WP-167).
    # Индекс + статус 'publishing' защищают от дублей при публикации в клуб.
    try:
        _m037 = _il.import_module("db.migrations.037_scheduled_post_dedup_lock")
        from db.connection import get_publication_pool as _get_publication_pool
        if await _m037.migrate_if_needed(await _get_publication_pool()):
            logger.info("✅ Migration 037: scheduled_post dedup lock applied")
        else:
            logger.info("✅ Migration 037: scheduled_post dedup lock already applied")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 037 (scheduled_post dedup lock) skipped: {_e}", exc_info=True)

    # Инициализация health BD таблиц (WP-268 Phase 5 G5, idempotent)
    from config.settings import HEALTH_URL
    if HEALTH_URL != DATABASE_URL:
        from db.models import create_tables_health
        from db.connection import get_health_pool
        health_pool = await get_health_pool()
        await create_tables_health(health_pool)

    # Параллельный прогрев всех Neon-пулов при старте (perf fix: устраняет lazy-init
    # внутри первого запроса пользователя, который добавлял 5-8 сек латентности).
    from db.connection import (
        get_pool, get_fsm_pool, get_persona_pool, get_subscription_pool,
        get_indicators_pool, get_learning_pool, get_rewards_pool, get_journal_pool,
        get_reference_pool, get_publication_pool, get_community_pool, get_lead_pool,
    )
    from config.settings import (
        FSM_URL, PERSONA_URL, SUBSCRIPTION_URL,
        INDICATORS_URL, LEARNING_URL, REWARDS_URL, JOURNAL_URL,
        REFERENCE_URL, PUBLICATION_URL, COMMUNITY_URL, LEAD_URL,
    )
    _pool_warmup = [
        (DATABASE_URL, get_pool, "Main"),
        (FSM_URL, get_fsm_pool, "FSM"),
        (PERSONA_URL, get_persona_pool, "Persona"),
        (SUBSCRIPTION_URL, get_subscription_pool, "Subscription"),
        (INDICATORS_URL, get_indicators_pool, "Indicators"),
        (LEARNING_URL, get_learning_pool, "Learning"),
        (REWARDS_URL, get_rewards_pool, "Rewards"),
        (JOURNAL_URL, get_journal_pool, "Journal"),
        (REFERENCE_URL, get_reference_pool, "Reference"),
        (PUBLICATION_URL, get_publication_pool, "Publication"),
        (COMMUNITY_URL, get_community_pool, "Community"),
        (LEAD_URL, get_lead_pool, "Lead"),
    ]
    _active_pools = [(name, fn) for url, fn, name in _pool_warmup if url]
    if _active_pools:
        _warmup_results = await asyncio.gather(
            *[fn() for _, fn in _active_pools], return_exceptions=True
        )
        _ok = sum(1 for r in _warmup_results if not isinstance(r, Exception))
        for (name, _), result in zip(_active_pools, _warmup_results):
            if isinstance(result, Exception):
                logger.warning(f"⚠️ Pool warm-up failed [{name}]: {result}")
        logger.info(f"✅ Пулы прогреты при старте: {_ok}/{len(_active_pools)}")

    # Eager init — eliminates first-request lazy-load latency (~600ms Langfuse, ~300ms i18n)
    from core.langfuse_client import init_langfuse
    from i18n.loader import init_i18n
    init_langfuse()
    init_i18n()
    logger.info("✅ Langfuse и i18n инициализированы при старте")


    # WP-341: helpdesk_tickets table
    try:
        import importlib as _il
        _m015 = _il.import_module("db.migrations.015_helpdesk_tickets")
        from db.connection import get_pool as _get_pool
        _created = await _m015.migrate_if_needed(await _get_pool())
        if _created:
            logger.info("✅ Migration 015: helpdesk_tickets created")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 015 skipped: {_e}")

    # retry_exhausted_date column (F2: остановка бесконечного retry-цикла после exhaustion)
    try:
        import importlib as _il
        _m017 = _il.import_module("db.migrations.017_retry_exhausted_date")
        _created = await _m017.migrate_if_needed(await _get_pool())
        if _created:
            logger.info("✅ Migration 017: retry_exhausted_date added to user_state")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 017 skipped: {_e}")

    # bot_recheck_at column (scheduler: отложенная recheck заблокированных пользователей)
    try:
        import importlib as _il
        _m018 = _il.import_module("db.migrations.018_bot_recheck_at")
        _created = await _m018.migrate_if_needed(await _get_pool())
        if _created:
            logger.info("✅ Migration 018: bot_recheck_at added to user_state")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 018 skipped: {_e}")

    # Projection DLQ table (Learning/Neon) — dead-letter queue for stalled events
    try:
        import importlib as _il
        _m019 = _il.import_module("db.migrations.019_projection_dlq")
        _created = await _m019.migrate_if_needed(await _get_pool())
        if _created:
            logger.info("✅ Migration 019: projection_dlq created in learning DB")
    except Exception as _e:
        logger.warning(f"⚠️ Migration 019 skipped: {_e}")

    # Learning-pool migrations (016, 025, 023, 024) moved to _bootstrap_learning_schema()
    # which runs as asyncio.create_task after the web server is up, so Railway healthcheck
    # is not blocked by the first-run table creation (migration 025 creates ~15 tables).

    # WP-253 G5: one-time ETL products /bot_data → reference.product
    from db.connection import get_bot_data_pool, get_reference_pool
    from db.migrations.migrate_products import migrate_products_if_needed
    try:
        _migrated = await migrate_products_if_needed(
            await get_bot_data_pool(), await get_reference_pool()
        )
        if _migrated:
            logger.info(f"✅ products ETL: {_migrated} строк → reference.product")
    except Exception as _e:
        logger.warning(f"⚠️ products ETL пропущен: {_e}")

    # Мониторинг ошибок (после init_db — нужен пул)
    from core.error_handler import setup_error_handler
    await setup_error_handler()

    # Загрузка токенов ЦД из DB (WP-82: token persistence)
    from clients.digital_twin import digital_twin
    dt_loaded = await digital_twin.load_tokens_from_db()
    if dt_loaded:
        logger.info(f"✅ DT: восстановлено {dt_loaded} подключений из DB")

    # Загрузка Ory tokens для Gateway MCP (WP-209 Ф0)
    from clients.gateway_mcp import gateway_mcp
    await gateway_mcp.load_tokens_from_db()
    logger.info("✅ Gateway MCP: Ory tokens загружены")

    # Bootstrap tool discovery cache (DP.SC.129). Fire-and-forget: ошибка не блокирует старт.
    # При недоступности Gateway бот работает с hardcoded tool set.
    try:
        discovered = await gateway_mcp.list_tools()
        logger.info(f"✅ Gateway MCP: discovery {len(discovered)} tools")
    except Exception as _e:
        logger.warning(f"Gateway MCP: tool discovery failed at startup, using hardcoded tools: {_e}")

    # Создаём bot с transport-layer Markdown→HTML intercept
    from core.safe_bot import SafeBot
    bot = SafeBot(token=BOT_TOKEN)

    # Инициализация State Machine (если включён флаг)
    state_machine = None
    if USE_STATE_MACHINE:
        try:
            from core.machine import StateMachine
            from config import BASE_DIR
            from states.registry import register_all_states
            from i18n import I18n

            state_machine = StateMachine()
            state_machine.load_transitions(BASE_DIR / "config" / "transitions.yaml")

            # Создаём зависимости для стейтов
            i18n = I18n()

            register_all_states(
                machine=state_machine,
                bot=bot,
                db=None,
                llm=None,
                i18n=i18n
            )

            logger.info(f"✅ StateMachine инициализирован ({len(state_machine._states)} стейтов)")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации StateMachine: {e}")
            import traceback
            traceback.print_exc()
            state_machine = None

    # Инициализация сервисного реестра
    from core.services_init import register_all_services
    register_all_services()
    logger.info("✅ ServiceRegistry инициализирован")

    # Центральный диспетчер — единая точка роутинга
    from core.dispatcher import Dispatcher as BotDispatcher
    bot_dispatcher = BotDispatcher(state_machine, bot)

    dp = Dispatcher(storage=PostgresStorage())

    # Global error handler: suppress transient Telegram API errors
    from aiogram.types import ErrorEvent
    from aiogram.exceptions import TelegramBadRequest

    @dp.error()
    async def on_telegram_error(event: ErrorEvent):
        exc = event.exception
        if isinstance(exc, TelegramBadRequest):
            msg = str(exc)
            if "query is too old" in msg or "query ID is invalid" in msg:
                # Callback query expired (>30s) — transient, safe to suppress
                logger.debug(f"[ErrorHandler] Suppressed stale callback query: {msg}")
                return True
            if "message is not modified" in msg:
                # User clicked same button twice — safe to suppress
                return True
        return False

    # Flood control: обрабатываем TelegramRetryAfter глобально
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.types import ErrorEvent

    @dp.errors()
    async def handle_flood_control(event: ErrorEvent) -> bool:
        if isinstance(event.exception, TelegramRetryAfter):
            retry_after = event.exception.retry_after
            logger.warning(f"[FloodControl] Telegram flood control, sleeping {retry_after}s")
            await asyncio.sleep(retry_after)
            return True  # handled
        return False  # propagate

    # Регистрируем middleware (порядок важен: Dedup → Maintenance → RateLimit → Logging → Passthrough → Tracing)
    # Dedup ПЕРВЫМ: webhook-retry (WP-7 incident 2026-07-10) должен отсекаться
    # до любой другой логики, иначе повторный update всё равно тратит DB round-trip.
    update_dedup = UpdateDedupMiddleware()
    dp.message.middleware(update_dedup)
    dp.callback_query.middleware(update_dedup)
    dp.message.middleware(MaintenanceMiddleware())
    dp.callback_query.middleware(MaintenanceMiddleware())
    rate_limiter = RateLimitMiddleware(max_messages=20, window_seconds=60)
    dp.message.middleware(rate_limiter)
    dp.callback_query.middleware(rate_limiter)
    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(ConsultationPassthroughMiddleware())
    dp.message.middleware(TracingMiddleware())
    dp.callback_query.middleware(TracingMiddleware())

    # === Порядок подключения роутеров (важен!) ===

    # 1. Роутеры режимов (mode_router)
    try:
        from engines.integration import setup_routers
        setup_routers(dp)
    except ImportError as e:
        logger.warning(f"⚠️ Не удалось загрузить engines: {e}.")

    # 2. Все хендлеры через handlers/ (commands, callbacks, settings, progress, etc.)
    from handlers import setup_handlers, setup_fallback
    setup_handlers(dp, bot_dispatcher)

    # 3. Fallback (catch-all) — ПОСЛЕДНИМ
    setup_fallback(dp)

    # Global fallback commands (T1 level — per-user tier menus set via sync_menu_commands)
    # WP-52: Global = minimal T1 fallback; per-user BotCommandScopeChat overrides this
    await bot.set_my_commands([
        BotCommand(command="learn", description="Марафон — получить урок"),
        BotCommand(command="train", description="Тренировка принципов"),
        BotCommand(command="test", description="Тест систематичности"),
        BotCommand(command="marathon_start", description="Начать марафон"),
        BotCommand(command="marathon_progress", description="Прогресс марафона"),
        BotCommand(command="marathon_stop", description="Остановить марафон"),
        BotCommand(command="me", description="Обо мне — дашборд и данные"),
        BotCommand(command="points", description="Баллы — баланс и начисления"),
        BotCommand(command="reflect", description="Рефлексия дня в личное руководство"),
        BotCommand(command="remind", description="Напоминание — /remind текст [время]"),
        BotCommand(command="consent", description="Согласие на трекинг развития"),
        BotCommand(command="features", description="Возможности платформы"),
        BotCommand(command="settings", description="Настройки и профиль"),
        BotCommand(command="support", description="Поддержка — открыть тикет"),
        BotCommand(command="connect_external", description="Подключить внешний AI-клиент (Claude Code и др.)"),
        BotCommand(command="my_clients", description="Активные внешние подключения"),
        BotCommand(command="status", description="Статус платформы"),
        BotCommand(command="help", description="Справка"),
    ])
    # WP-440: per-language Bot Menu commands registered only when multilingual
    # is enabled (track A bot is Russian-only; non-ru locales fall back to the
    # default Russian menu set above).
    if MULTILANG_ENABLED:
        await bot.set_my_commands([
            BotCommand(command="learn", description="Marathon — get a lesson"),
            BotCommand(command="train", description="Principles training"),
            BotCommand(command="test", description="Systematicity test"),
            BotCommand(command="marathon_start", description="Start marathon"),
            BotCommand(command="marathon_progress", description="Marathon progress"),
            BotCommand(command="marathon_stop", description="Stop marathon"),
            BotCommand(command="me", description="About me — dashboard & data"),
            BotCommand(command="points", description="Points — balance & log"),
            BotCommand(command="reflect", description="Daily reflection to personal guide"),
            BotCommand(command="remind", description="Reminder — /remind text [time]"),
            BotCommand(command="consent", description="Tracking consent"),
            BotCommand(command="features", description="Platform features"),
            BotCommand(command="settings", description="Settings & profile"),
            BotCommand(command="connect_external", description="Connect external AI client (Claude Code etc.)"),
            BotCommand(command="my_clients", description="Active external connections"),
            BotCommand(command="status", description="Platform status"),
            BotCommand(command="help", description="Help"),
        ], language_code="en")
        await bot.set_my_commands([
            BotCommand(command="learn", description="Maratón — obtener lección"),
            BotCommand(command="train", description="Entrenamiento de principios"),
            BotCommand(command="test", description="Test de sistematicidad"),
            BotCommand(command="me", description="Sobre mí — panel y datos"),
            BotCommand(command="points", description="Puntos — saldo y registro"),
            BotCommand(command="reflect", description="Reflexión diaria en guía personal"),
            BotCommand(command="consent", description="Consentimiento de seguimiento"),
            BotCommand(command="features", description="Funciones de la plataforma"),
            BotCommand(command="settings", description="Ajustes y perfil"),
            BotCommand(command="status", description="Estado de la plataforma"),
            BotCommand(command="help", description="Ayuda"),
        ], language_code="es")
        await bot.set_my_commands([
            BotCommand(command="learn", description="Marathon — obtenir une leçon"),
            BotCommand(command="train", description="Entraînement des principes"),
            BotCommand(command="test", description="Test de systématicité"),
            BotCommand(command="me", description="À propos de moi — tableau de bord"),
            BotCommand(command="points", description="Points — solde et journal"),
            BotCommand(command="reflect", description="Réflexion quotidienne — guide personnel"),
            BotCommand(command="consent", description="Consentement au suivi"),
            BotCommand(command="features", description="Fonctionnalités"),
            BotCommand(command="settings", description="Paramètres et profil"),
            BotCommand(command="status", description="Statut de la plateforme"),
            BotCommand(command="help", description="Aide"),
        ], language_code="fr")
        await bot.set_my_commands([
            BotCommand(command="learn", description="马拉松 — 获取课程"),
            BotCommand(command="train", description="原则训练"),
            BotCommand(command="test", description="系统性测试"),
            BotCommand(command="me", description="关于我 — 仪表板和数据"),
            BotCommand(command="points", description="积分 — 余额与记录"),
            BotCommand(command="reflect", description="每日反思 — 个人指南"),
            BotCommand(command="consent", description="发展追踪同意"),
            BotCommand(command="features", description="平台功能"),
            BotCommand(command="settings", description="设置与档案"),
            BotCommand(command="status", description="平台状态"),
            BotCommand(command="help", description="帮助"),
        ], language_code="zh")

    # Команды разработчика (отдельное меню)
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    if dev_chat_id:
        try:
            await bot.set_my_commands([
                BotCommand(command="stats", description="Пользователи и активность"),
                BotCommand(command="usage", description="Популярность сервисов"),
                BotCommand(command="qa", description="Качество консультаций"),
                BotCommand(command="health", description="Состояние системы"),
                BotCommand(command="latency", description="Латентность (светофор)"),
                BotCommand(command="errors", description="Ошибки (24h)"),
                BotCommand(command="analytics", description="Сводная аналитика"),
                BotCommand(command="delivery", description="Доставка уроков марафона"),
                BotCommand(command="reports", description="Баг-репорты"),
                BotCommand(command="tailor", description="Занятие Портного (WP-149)"),
                BotCommand(command="dt_sync", description="Sync engagement → digital twins"),
                BotCommand(command="user_repair", description="Диагностика/починка GitHub-интеграции пользователя"),
                BotCommand(command="reset", description="Full wipe тестера → ре-онбординг"),
                BotCommand(command="waka", description="WakaTime статистика"),
                BotCommand(command="mode", description="Главное меню"),
                BotCommand(command="help", description="Справка"),
            ], scope=BotCommandScopeChat(chat_id=int(dev_chat_id)))
        except Exception as e:
            logger.warning(f"Could not set dev commands: {e}")

    # Запуск планировщика
    from core.scheduler import init_scheduler
    init_scheduler(bot_dispatcher, dp, BOT_TOKEN)

    # Запуск бота: webhook (prod) или polling (dev)
    from config.settings import WEBHOOK_URL, WEBHOOK_SECRET, WEBHOOK_PATH, PORT

    from oauth_server import set_bot_instance, create_oauth_app, start_oauth_server, stop_oauth_server
    set_bot_instance(bot)

    if WEBHOOK_URL:
        # ═══ Webhook mode (production) ═══
        logger.info(f"🌐 Webhook mode: {WEBHOOK_URL}{WEBHOOK_PATH} on port {PORT}")

        app = create_oauth_app(dp=dp, bot=bot)

        # Start web server FIRST so Railway health check passes immediately
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)  # nosec B104
        await site.start()
        logger.info(f"✅ Web server listening on port {PORT}")

        # Bootstrap learning schema in background — healthcheck is already passing above
        asyncio.create_task(_bootstrap_learning_schema())

        # Register webhook with Telegram (secret already sanitized/generated in settings.py)
        webhook_ok = False
        from aiogram.exceptions import TelegramRetryAfter as _TRAfter
        for _attempt in range(4):  # up to 3 retries for flood control
            try:
                await bot.set_webhook(
                    url=f"{WEBHOOK_URL}{WEBHOOK_PATH}",
                    secret_token=WEBHOOK_SECRET,
                    drop_pending_updates=False,
                    allowed_updates=[
                        "message", "callback_query", "inline_query",
                        "channel_post", "my_chat_member",
                        "chat_member", "chat_join_request", "edited_message",
                        "pre_checkout_query", "shipping_query",
                    ],
                )
                # Verify webhook is reachable (getWebhookInfo diagnostic)
                info = await bot.get_webhook_info()
                logger.info(
                    f"✅ Webhook registered: url={info.url}, "
                    f"pending={info.pending_update_count}, "
                    f"last_error={info.last_error_message or 'none'}, "
                    f"secret={'set' if WEBHOOK_SECRET else 'none'}"
                )
                if info.last_error_message:
                    logger.warning(f"⚠️ Telegram reports webhook error: {info.last_error_message}")
                webhook_ok = True
                break
            except _TRAfter as e:
                wait = e.retry_after + 1
                if _attempt < 3:
                    logger.warning(f"⚠️ SetWebhook flood control, retry in {wait}s (attempt {_attempt + 1}/3)")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"❌ SetWebhook flood control: retries exhausted")
            except Exception as e:
                logger.error(f"❌ Failed to set webhook: {e}")
                try:
                    from core.error_logger import log_error
                    await log_error(
                        error_type="webhook_registration",
                        message=str(e),
                        context={"url": WEBHOOK_URL, "has_secret": bool(WEBHOOK_SECRET)},
                    )
                except Exception:
                    pass
                break

        if webhook_ok:
            logger.info("🚀 Бот запущен (webhook) с PostgreSQL!")

            # Re-register webhook after delay to survive rolling deploy.
            # Old container's shutdown may delete_webhook before this runs;
            # this re-registration restores it.
            async def _reregister_webhook():
                await asyncio.sleep(30)
                try:
                    await bot.set_webhook(
                        url=f"{WEBHOOK_URL}{WEBHOOK_PATH}",
                        secret_token=WEBHOOK_SECRET,
                        drop_pending_updates=False,
                        allowed_updates=[
                            "message", "callback_query", "inline_query",
                            "channel_post", "my_chat_member",
                            "chat_member", "chat_join_request", "edited_message",
                            "pre_checkout_query", "shipping_query",
                        ],
                    )
                    logger.info("✅ Webhook re-registered (post-deploy safety)")
                except Exception as e:
                    logger.error(f"❌ Webhook re-registration failed: {e}")

            asyncio.create_task(_reregister_webhook())

            # WP-358 Ф10.5: recovery scan + periodic loop для orphan'ов финализации
            try:
                from handlers.external_session import (
                    recover_orphan_finalizations,
                    _periodic_recovery_loop,
                    _RECOVERY_PERIODIC_INTERVAL_SEC,
                )
                asyncio.create_task(recover_orphan_finalizations(bot))
                asyncio.create_task(_periodic_recovery_loop(bot, _RECOVERY_PERIODIC_INTERVAL_SEC))
                logger.info(
                    "[session] recovery scan scheduled + periodic re-scan every %ds (WP-358 Ф10.5)",
                    _RECOVERY_PERIODIC_INTERVAL_SEC,
                )
            except Exception as e:
                logger.warning(f"[session] recovery scan skip: {type(e).__name__}: {e}")

            # WP-7 TGSH7: boot recovery — restart heartbeat-pollers for active FSM sessions.
            try:
                from handlers.external_session import recover_active_heartbeat_pollers
                asyncio.create_task(recover_active_heartbeat_pollers(bot))
                logger.info("[heartbeat] active-session boot recovery scheduled (WP-7 TGSH7)")
            except Exception as e:
                logger.warning(f"[heartbeat] boot recovery skip: {type(e).__name__}: {e}")

            # Keep running until shutdown signal
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)
            await stop_event.wait()

            # Graceful shutdown — do NOT delete webhook on SIGTERM.
            # During rolling deploy, new container already registered the same
            # webhook URL. Calling delete_webhook here would remove it, leaving
            # Telegram with no webhook → "stuck buttons" until next redeploy.
            logger.info("🛑 Shutting down (webhook preserved for rolling deploy)")
            # WP-358 Ф10.5 Medium: drain in-flight finalize tasks ДО runner.cleanup
            try:
                from handlers.external_session import cancel_bg_tasks
                drained = await cancel_bg_tasks(timeout=5.0)
                logger.info(f"[session] drained {drained} bg tasks before shutdown")
            except Exception as e:
                logger.warning(f"[session] cancel_bg_tasks failed: {type(e).__name__}: {e}")
            await runner.cleanup()
        else:
            # Fallback to polling if webhook registration failed
            logger.warning("⚠️ Webhook failed, falling back to polling mode")
            await runner.cleanup()
            await bot.delete_webhook(drop_pending_updates=False)
            logger.info("🚀 Бот запущен (polling fallback) с PostgreSQL!")
            await dp.start_polling(bot)
    else:
        # ═══ Polling mode (local development) ═══
        logger.info("📡 Polling mode (no WEBHOOK_URL set)")

        oauth_runner = None
        try:
            oauth_runner = await start_oauth_server()
        except Exception as e:
            logger.error(f"⚠️ Ошибка запуска OAuth сервера: {e}")

        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("🚀 Бот запущен (polling) с PostgreSQL!")

        try:
            await dp.start_polling(bot)
        finally:
            if oauth_runner:
                await stop_oauth_server(oauth_runner)

    # Cleanup (both modes)
    from clients.claude import ClaudeClient
    from clients.mcp import MCPClient
    from clients.gateway_mcp import gateway_mcp
    from clients.wakatime import wakatime_client
    await ClaudeClient.close_session()
    await MCPClient.close_session()
    await gateway_mcp.close()
    await wakatime_client.close_session()
    # ФИКС (peer-session 2026-06-05-02): singleton-сессии aisystant/discourse/
    # github_content не закрывались нигде → "Unclosed client session/connector"
    # при остановке процесса. Закрываем все три (discourse/github_content
    # создаются условно → null-guard).
    from clients.aisystant import aisystant
    from clients.discourse import discourse
    from clients.github_content import github_content
    await aisystant.close()
    if discourse:
        await discourse.close()
    if github_content:
        await github_content.close()
    logger.info("🔒 HTTP sessions закрыты")

    # Langfuse flush (WP-179 Ф3)
    from core.langfuse_client import langfuse_flush
    langfuse_flush()

    from core.error_handler import shutdown_error_handler
    await shutdown_error_handler()

if __name__ == "__main__":
    asyncio.run(main())
