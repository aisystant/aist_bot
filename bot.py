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
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

# Feature flags
from config import USE_STATE_MACHINE

# Импорты из модульных компонентов
from clients.mcp import mcp_guides, mcp_knowledge, mcp
from clients.claude import ClaudeClient
from db import init_db
from db.queries import get_intern, update_intern, get_topics_today
from integrations.telegram.keyboards import kb_update_profile, progress_bar

# ============= КОНФИГУРАЦИЯ =============

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MCP_URL = os.getenv("MCP_URL", "https://guides-mcp.aisystant.workers.dev/mcp")
KNOWLEDGE_MCP_URL = os.getenv("KNOWLEDGE_MCP_URL", "https://knowledge-mcp.aisystant.workers.dev/mcp")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не установлен!")
if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY не установлен!")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не установлен!")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Московское время (UTC+3)
MOSCOW_TZ = timezone(timedelta(hours=3))

def moscow_now() -> datetime:
    """Получить текущее время по Москве"""
    return datetime.now(MOSCOW_TZ)

def moscow_today():
    """Получить текущую дату по Москве"""
    return moscow_now().date()

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
from core.middleware import LoggingMiddleware

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

    # Создаём bot раньше, чтобы передать в State Machine
    bot = Bot(token=BOT_TOKEN)

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

    # Центральный диспетчер — единая точка роутинга
    from core.dispatcher import Dispatcher as BotDispatcher
    bot_dispatcher = BotDispatcher(state_machine, bot)

    dp = Dispatcher(storage=PostgresStorage())

    # Регистрируем middleware для логирования
    dp.message.middleware(LoggingMiddleware())

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

    # Установка команд бота для разных языков
    # Русский (по умолчанию)
    await bot.set_my_commands([
        BotCommand(command="learn", description="Получить новую тему"),
        BotCommand(command="progress", description="Мой прогресс"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="update", description="Настройки"),
        BotCommand(command="mode", description="Выбор режима"),
        BotCommand(command="language", description="Сменить язык"),
        BotCommand(command="start", description="Перезапустить онбординг"),
        BotCommand(command="twin", description="Цифровой двойник"),
        BotCommand(command="help", description="Справка")
    ])

    # Английский
    await bot.set_my_commands([
        BotCommand(command="learn", description="Get a new topic"),
        BotCommand(command="progress", description="My progress"),
        BotCommand(command="profile", description="My profile"),
        BotCommand(command="update", description="Settings"),
        BotCommand(command="mode", description="Select mode"),
        BotCommand(command="language", description="Change language"),
        BotCommand(command="start", description="Restart onboarding"),
        BotCommand(command="twin", description="Digital Twin"),
        BotCommand(command="help", description="Help")
    ], language_code="en")

    # Испанский
    await bot.set_my_commands([
        BotCommand(command="learn", description="Obtener un nuevo tema"),
        BotCommand(command="progress", description="Mi progreso"),
        BotCommand(command="profile", description="Mi perfil"),
        BotCommand(command="update", description="Configuración"),
        BotCommand(command="mode", description="Seleccionar modo"),
        BotCommand(command="language", description="Cambiar idioma"),
        BotCommand(command="start", description="Reiniciar onboarding"),
        BotCommand(command="twin", description="Gemelo Digital"),
        BotCommand(command="help", description="Ayuda")
    ], language_code="es")

    # Французский
    await bot.set_my_commands([
        BotCommand(command="learn", description="Obtenir un nouveau sujet"),
        BotCommand(command="progress", description="Mon progrès"),
        BotCommand(command="profile", description="Mon profil"),
        BotCommand(command="update", description="Paramètres"),
        BotCommand(command="mode", description="Sélectionner le mode"),
        BotCommand(command="language", description="Changer de langue"),
        BotCommand(command="start", description="Recommencer l'inscription"),
        BotCommand(command="help", description="Aide")
    ], language_code="fr")

    # Запуск планировщика
    from core.scheduler import init_scheduler
    init_scheduler(bot_dispatcher, dp, BOT_TOKEN)

    # Запуск OAuth сервера (для Linear интеграции)
    oauth_runner = None
    try:
        from oauth_server import start_oauth_server, set_bot_instance, stop_oauth_server
        set_bot_instance(bot)
        oauth_runner = await start_oauth_server()
    except ImportError:
        logger.warning("⚠️ oauth_server не найден, Linear интеграция отключена")
    except Exception as e:
        logger.error(f"⚠️ Ошибка запуска OAuth сервера: {e}")

    logger.info("🚀 Бот запущен с PostgreSQL!")

    try:
        await dp.start_polling(bot)
    finally:
        if oauth_runner:
            await stop_oauth_server(oauth_runner)

if __name__ == "__main__":
    asyncio.run(main())
