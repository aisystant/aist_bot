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
from config import USE_STATE_MACHINE

# Импорты из модульных компонентов
from clients.mcp import mcp_knowledge
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
from core.middleware import MaintenanceMiddleware, LoggingMiddleware, TracingMiddleware

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

async def main():
    global state_machine

    # Инициализация БД
    await init_db()

    # Мониторинг ошибок (после init_db — нужен пул)
    from core.error_handler import setup_error_handler
    await setup_error_handler()

    # Загрузка токенов ЦД из DB (WP-82: token persistence)
    from clients.digital_twin import digital_twin
    dt_loaded = await digital_twin.load_tokens_from_db()
    if dt_loaded:
        logger.info(f"✅ DT: восстановлено {dt_loaded} подключений из DB")

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

    # Регистрируем middleware (порядок важен: Maintenance → Logging → Tracing)
    dp.message.middleware(MaintenanceMiddleware())
    dp.callback_query.middleware(MaintenanceMiddleware())
    dp.message.middleware(LoggingMiddleware())
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
        BotCommand(command="progress", description="Мой прогресс"),
        BotCommand(command="features", description="Возможности платформы"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="help", description="Справка"),
    ])
    await bot.set_my_commands([
        BotCommand(command="learn", description="Marathon — get a lesson"),
        BotCommand(command="train", description="Principles training"),
        BotCommand(command="test", description="Systematicity test"),
        BotCommand(command="progress", description="My progress"),
        BotCommand(command="features", description="Platform features"),
        BotCommand(command="profile", description="My profile"),
        BotCommand(command="help", description="Help"),
    ], language_code="en")
    await bot.set_my_commands([
        BotCommand(command="learn", description="Maratón — obtener lección"),
        BotCommand(command="train", description="Entrenamiento de principios"),
        BotCommand(command="test", description="Test de sistematicidad"),
        BotCommand(command="progress", description="Mi progreso"),
        BotCommand(command="features", description="Funciones de la plataforma"),
        BotCommand(command="profile", description="Mi perfil"),
        BotCommand(command="help", description="Ayuda"),
    ], language_code="es")
    await bot.set_my_commands([
        BotCommand(command="learn", description="Marathon — obtenir une leçon"),
        BotCommand(command="train", description="Entraînement des principes"),
        BotCommand(command="test", description="Test de systématicité"),
        BotCommand(command="progress", description="Mon progrès"),
        BotCommand(command="features", description="Fonctionnalités"),
        BotCommand(command="profile", description="Mon profil"),
        BotCommand(command="help", description="Aide"),
    ], language_code="fr")
    await bot.set_my_commands([
        BotCommand(command="learn", description="马拉松 — 获取课程"),
        BotCommand(command="train", description="原则训练"),
        BotCommand(command="test", description="系统性测试"),
        BotCommand(command="progress", description="我的进度"),
        BotCommand(command="features", description="平台功能"),
        BotCommand(command="profile", description="我的档案"),
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
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"✅ Web server listening on port {PORT}")

        # Register webhook with Telegram (secret already sanitized/generated in settings.py)
        webhook_ok = False
        try:
            await bot.set_webhook(
                url=f"{WEBHOOK_URL}{WEBHOOK_PATH}",
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=False,
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
        except Exception as e:
            logger.error(f"❌ Failed to set webhook: {e}")
            # Log to error_logs for persistent diagnostics
            try:
                from core.error_logger import log_error
                await log_error(
                    error_type="webhook_registration",
                    message=str(e),
                    context={"url": WEBHOOK_URL, "has_secret": bool(WEBHOOK_SECRET)},
                )
            except Exception:
                pass

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
                    )
                    logger.info("✅ Webhook re-registered (post-deploy safety)")
                except Exception as e:
                    logger.error(f"❌ Webhook re-registration failed: {e}")

            asyncio.create_task(_reregister_webhook())

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
    await ClaudeClient.close_session()
    await MCPClient.close_session()
    logger.info("🔒 HTTP sessions закрыты")

    from core.error_handler import shutdown_error_handler
    await shutdown_error_handler()

if __name__ == "__main__":
    asyncio.run(main())
