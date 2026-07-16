"""
Конфигурация и настройки бота.

Все константы, токены, настройки собраны в одном месте.
"""

import os
import logging
from datetime import timedelta, timezone
from pathlib import Path

# ============= ТОКЕНЫ И ПОДКЛЮЧЕНИЯ =============

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# WP-384 Ф2: OpenAI Whisper для голосового ввода (DP.SC.178)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
VOICE_MAX_DURATION_SEC = int(os.getenv("VOICE_MAX_DURATION_SEC", "60"))

# Claude models: Sonnet для сложных задач, Haiku для простых
CLAUDE_MODEL_OPUS = "claude-sonnet-4-6"
CLAUDE_MODEL_SONNET = "claude-sonnet-4-6"
CLAUDE_MODEL_HAIKU = "claude-haiku-4-5-20251001"
DATABASE_URL = os.getenv("DATABASE_URL")

# WP-253 tech debt bridge: products + finance_payments до ETL в Neon (G3/G5).
# DT_DATABASE_URL = Railway /bot_data — содержит public.products + public.finance_payments.
# После ETL products → reference + finance_payments → payment удалить этот pool.
BOT_DATA_URL = os.getenv("DT_DATABASE_URL") or os.getenv("DATABASE_URL")

# WP-269 read-path migration (cut-over deploy 26 апр): новые per-domain БД.
# После DROP legacy БД бот читает из этих pools.
# Fallback на DATABASE_URL только для локального dev — в production должны быть set.
PERSONA_URL = os.getenv("PERSONA_URL") or os.getenv("DATABASE_URL")  # persona.ory_identity, persona.identity_map
SUBSCRIPTION_URL = os.getenv("SUBSCRIPTION_URL") or os.getenv("DATABASE_URL")  # subscription.contract
INDICATORS_URL = os.getenv("INDICATORS_URL") or os.getenv("DATABASE_URL")  # indicators.calculated_profile (заменяет digitaltwin)
LEARNING_URL = os.getenv("LEARNING_URL") or os.getenv("DATABASE_URL")  # learning.domain_event (qa, notifications, traces)
REWARDS_URL = os.getenv("REWARDS_URL") or os.getenv("DATABASE_URL")  # rewards.point_balances (WP-253 Ф9.3, проекция баллов)
# WP-188 Ф17: учёт opt-in/opt-out на трекинг (learning.tracking_consent) через role consent_writer
# (миграция 113). Отдельный pool — write-pool для GDPR-границы. Fallback на LEARNING_URL — read-only path.
CONSENT_URL = os.getenv("CONSENT_URL") or os.getenv("LEARNING_URL") or os.getenv("DATABASE_URL")

# WP-268 Phase 3 Block 1: fsm_states вынос в Railway-local Postgres (паттерн DP.ARCH.004 §10.10).
# В production должно быть set явно. Fallback на DATABASE_URL — только для локального dev.
FSM_URL = os.getenv("FSM_URL") or os.getenv("DATABASE_URL")

# WP-268 Phase 3 Block 2: qa_history + feedback_triage вынос в Neon БД `journal` (DP.ARCH.004 §3.2).
# Категория WP-257: Память.Observed (session events). PII content (Q&A текст).
JOURNAL_URL = os.getenv("JOURNAL_URL") or os.getenv("DATABASE_URL")

# WP-268 Phase 5 G5 Tier2: error_logs, user_sessions, pending_fixes → Neon health БД (DP.ARCH.004 §8).
# Наблюдаемость системы и сессии. Health BD — special (не entity).
HEALTH_URL = os.getenv("HEALTH_URL") or os.getenv("DATABASE_URL")

# WP-253 Пробел C: OAuth-токены интеграций (GitHub, etc.) — Neon secrets БД.
# DP.ARCH.004 §B7.3.1: secrets ∩ PII → pgcrypto column-level + RLS.
# Fallback на DATABASE_URL только для локального dev; в production обязателен.
SECRETS_URL = os.getenv("SECRETS_URL") or os.getenv("DATABASE_URL")

# WP-253 lift-and-shift bot_data → 12 BC БД (8 мая 2026):
# Дополнительные per-BC БД для оставшихся таблиц bot_data.
PUBLICATION_URL = os.getenv("PUBLICATION_URL") or os.getenv("DATABASE_URL")  # scheduled_post, published_post, channel_monitor, channel_mention_log
COMMUNITY_URL = os.getenv("COMMUNITY_URL") or os.getenv("DATABASE_URL")      # club_account (discourse), mentorship
LEAD_URL = os.getenv("LEAD_URL") or os.getenv("DATABASE_URL")                # conversion_event, funnel_record, claim
REFERENCE_URL = os.getenv("REFERENCE_URL") or os.getenv("DATABASE_URL")      # product, training_setting, training_child, tariffs

# Ключ для pgp_sym_encrypt/decrypt токенов в secrets БД.
# В production: Railway env var GITHUB_TOKEN_ENCRYPTION_KEY (случайный hex, ≥32 байта).
GITHUB_TOKEN_ENCRYPTION_KEY = os.getenv("GITHUB_TOKEN_ENCRYPTION_KEY", "")
KNOWLEDGE_MCP_URL = os.getenv("KNOWLEDGE_MCP_URL", "https://knowledge-mcp.aisystant.workers.dev/mcp")
DIGITAL_TWIN_MCP_URL = os.getenv("DIGITAL_TWIN_MCP_URL", "https://twin.aisystant.com/mcp")
GATEWAY_MCP_URL = os.getenv("GATEWAY_MCP_URL", "https://mcp.aisystant.com/mcp")
GATEWAY_MCP_TIMEOUT: int = int(os.getenv("GATEWAY_MCP_TIMEOUT", "3"))

# ============= LANGFUSE (L5 Observability, WP-179) =============
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

# ============= LINEAR OAUTH (тестовая интеграция) =============
# Временный код для тестирования OAuth flow перед Digital Twin
LINEAR_CLIENT_ID = os.getenv("LINEAR_CLIENT_ID")
LINEAR_CLIENT_SECRET = os.getenv("LINEAR_CLIENT_SECRET")
LINEAR_REDIRECT_URI = os.getenv("LINEAR_REDIRECT_URI", "https://aistmebot-production.up.railway.app/auth/linear/callback")
PORT = int(os.getenv("PORT", os.getenv("OAUTH_SERVER_PORT", "8080")))
OAUTH_SERVER_PORT = PORT  # backwards compat

# ============= WEBHOOK (WP-44) =============
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://xxx.up.railway.app (empty = polling mode)
WEBHOOK_PATH = "/telegram"

# Webhook secret: Telegram allows only A-Za-z0-9_- (1-256 chars)
# Auto-generate from BOT_TOKEN hash if not set — deterministic, always valid
import hashlib as _hashlib
import re as _re
_raw_webhook_secret = os.getenv("WEBHOOK_SECRET", "")
if _raw_webhook_secret:
    # Sanitize user-provided secret
    WEBHOOK_SECRET = _re.sub(r'[^A-Za-z0-9_\-]', '', _raw_webhook_secret) or None
else:
    # Auto-generate: sha256(bot_token)[:48] — hex chars, always valid
    WEBHOOK_SECRET = _hashlib.sha256((BOT_TOKEN or "").encode()).hexdigest()[:48] if BOT_TOKEN else None

# ============= ORY OAUTH (WP-187 — бот+Ory) =============
ORY_BASE_URL = os.getenv("ORY_BASE_URL", "https://auth.system-school.ru/hydra")
ORY_CLIENT_ID = os.getenv("ORY_CLIENT_ID")
ORY_CLIENT_SECRET = os.getenv("ORY_CLIENT_SECRET")
ORY_REDIRECT_URI = os.getenv("ORY_REDIRECT_URI", "https://aistmebot-production.up.railway.app/auth/ory/callback")

# ============= YOOKASSA (WP-181 Ф7) =============
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")

# ============= AISYSTANT LMS (WP-79) =============
AISYSTANT_BASE_URL = os.getenv("AISYSTANT_BASE_URL", "https://aisystant.system-school.ru")
AISYSTANT_TECH_PASSWORD = os.getenv("AISYSTANT_TECH_PASSWORD", "")
# LMS DB — прямое подключение для квалификаций и CRM-данных (WP-151)
LMS_DATABASE_URL = os.getenv("LMS_DATABASE_URL", "")

# ============= GITHUB OAUTH =============
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI", "https://aistmebot-production.up.railway.app/auth/github/callback")

# ============= GOOGLE CALENDAR OAUTH (WP-128) =============
GOOGLE_CALENDAR_CLIENT_ID = os.getenv("GOOGLE_CALENDAR_CLIENT_ID")
GOOGLE_CALENDAR_CLIENT_SECRET = os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET")
GOOGLE_CALENDAR_REDIRECT_URI = os.getenv("GOOGLE_CALENDAR_REDIRECT_URI", "https://aistmebot-production.up.railway.app/auth/google-calendar/callback")

# ============= WAKATIME OAUTH (WP-109) =============
WAKATIME_CLIENT_ID = os.getenv("WAKATIME_CLIENT_ID")
WAKATIME_CLIENT_SECRET = os.getenv("WAKATIME_CLIENT_SECRET")
WAKATIME_REDIRECT_URI = os.getenv("WAKATIME_REDIRECT_URI", "https://aistmebot-production.up.railway.app/auth/wakatime/callback")

# ============= L2 AUTO-FIX (WP-45 Phase 3) =============
GITHUB_BOT_PAT = os.getenv("GITHUB_BOT_PAT")
AUTOFIX_REPO = os.getenv("AUTOFIX_REPO", "aisystant/aist_bot")
AUTOFIX_BRANCH_BASE = os.getenv("AUTOFIX_BRANCH_BASE", "new-architecture")
AUTOFIX_BOT_DIR = ""  # repo root = code root (no subdirectory)
AUTOFIX_MAX_FILES = 3
AUTOFIX_MAX_PROPOSALS = 3  # per 15-min cycle
AUTOFIX_PROTECTED = frozenset({
    "db/models.py", "core/scheduler.py", "bot.py",
    "config/settings.py", "config/__init__.py",
})

def validate_env():
    """Проверка наличия обязательных переменных окружения"""
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN не установлен!")
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY не установлен!")
    # WP-268 Phase 4: DATABASE_URL теперь Railway-local Postgres bot_data (не Neon aist_bot).
    # Per-domain URLs (LEARNING_URL, PERSONA_URL, etc.) — основные пути после cut-over.
    # DATABASE_URL остаётся обязательным для legacy bot_data таблиц (users, marathon_content, ...).
    if not DATABASE_URL:
        import logging as _log
        _log.getLogger(__name__).warning("DATABASE_URL не установлен — bot_data таблицы недоступны")

# ============= FEATURE FLAGS =============

# State Machine: включает новую архитектуру
# Когда True — используется StateMachine вместо старых хэндлеров
# По умолчанию False для обратной совместимости
USE_STATE_MACHINE = os.getenv("USE_STATE_MACHINE", "true").lower() == "true"

# Multilingual UI: when False (default), the bot is Russian-only and the
# language picker is hidden. WHY: this is the Russia track (track A) bot; its
# non-Russian locales (es/fr/zh) are unreviewed machine output and ~10 code
# paths bypass i18n, so switching off Russian shows a Russian/English/machine
# mix. Serious multilingual belongs to the world track (track B). Flip to "1"
# to restore the picker without touching logic. See WP-440.
MULTILANG_ENABLED = os.getenv("MULTILANG_ENABLED", "0") == "1"

# Maintenance mode: блокирует всех кроме ALLOWED_TESTERS
# Используется для тестовых ботов, чтобы пользователи не пользовались ими как основными
MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"
MAINTENANCE_REDIRECT_BOT = os.getenv("MAINTENANCE_REDIRECT_BOT", "@aist_me_bot")
BOT_USERNAME = os.getenv("BOT_USERNAME", "aist_me_bot")

# Список разрешённых chat_id через запятую: "123456,789012"
_allowed = os.getenv("ALLOWED_TESTERS", "")
ALLOWED_TESTERS: set[int] = {int(x.strip()) for x in _allowed.split(",") if x.strip().isdigit()}

# Telegram ID разработчика — освобождён от rate limiting
DEVELOPER_CHAT_ID: int = int(os.getenv("DEVELOPER_CHAT_ID", "0"))

# Telegram ID канала наставников марафона — алерты о пропусках и failed отправках
MENTOR_CHANNEL_ID: int = int(os.getenv("MENTOR_CHANNEL_ID", "0"))

# ============= EVENT GATEWAY (WP-268 Phase 2 dual-write) =============
# Cloudflare Worker, принимающий доменные события от бота
# Dual-write: legacy DB (источник истины) + event-gateway (fire-and-forget)
EVENT_GATEWAY_URL: str = os.getenv("EVENT_GATEWAY_URL", "https://event-gateway.aisystant.workers.dev")
EVENT_GATEWAY_TIMEOUT: float = float(os.getenv("EVENT_GATEWAY_TIMEOUT", "5.0"))
# Feature flag: можно отключить dual-write на проде через env var
EVENT_GATEWAY_ENABLED: bool = os.getenv("EVENT_GATEWAY_ENABLED", "true").lower() == "true"

# WP-418 Ф3/Ф4: единый слой доставки (Доставщик, core.notification_service).
# Выключен по умолчанию. Включать ДО миграции/деплоя точек-отправителей: drain на
# пустой очереди безвреден, а мигрированная точка без drain молча копит сообщения
# в очереди (сторож _watch_delivery_queue заалертит через 10 мин). В проде env
# выставляется ДО merge волны миграции — для кода без этой ветки переменная no-op.
DELIVERY_LAYER_ENABLED: bool = os.getenv("DELIVERY_LAYER_ENABLED", "false").lower() == "true"

# ============= ЛОГИРОВАНИЕ =============
# logging.basicConfig() вызывается в bot.py (единая точка конфигурации)

def get_logger(name: str) -> logging.Logger:
    """Получить логгер для модуля"""
    return logging.getLogger(name)

# ============= ВРЕМЕННАЯ ЗОНА =============

MOSCOW_TZ = timezone(timedelta(hours=3))

# ============= ПУТИ К ФАЙЛАМ =============

BASE_DIR = Path(__file__).parent.parent
TOPICS_DIR = BASE_DIR / "topics"
KNOWLEDGE_STRUCTURE_PATH = BASE_DIR / "knowledge_structure.yaml"
CHANNEL_CONTEXTS_PATH = BASE_DIR / "config" / "channel_contexts.yaml"

# ============= КОНТЕКСТЫ КАНАЛОВ (SC.118) =============

def _load_channel_contexts() -> dict:
    """Загрузить описания каналов из YAML."""
    import re
    import yaml
    path = CHANNEL_CONTEXTS_PATH
    if not path.exists():
        return {"default": {}, "channels": []}
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    # Прекомпилировать regex паттерны
    for ch in data.get("channels", []):
        pattern = ch.get("title_pattern", "")
        if pattern:
            ch["_compiled_pattern"] = re.compile(pattern, re.IGNORECASE)
    return data

CHANNEL_CONTEXTS = _load_channel_contexts()


def get_channel_context(channel_title: str) -> dict:
    """Найти контекст канала по названию. Fallback на default."""
    for ch in CHANNEL_CONTEXTS.get("channels", []):
        compiled = ch.get("_compiled_pattern")
        if compiled and compiled.search(channel_title):
            return ch
    return CHANNEL_CONTEXTS.get("default", {})

# ============= РЕЖИМЫ РАБОТЫ =============

class Mode:
    """Режимы работы бота"""
    MARATHON = "marathon"
    FEED = "feed"
    TRAINING = "training"
    BOTH = "both"

# ============= СТАТУСЫ =============

class MarathonStatus:
    """Статусы марафона"""
    NOT_STARTED = "not_started"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"

class FeedStatus:
    """Статусы ленты"""
    NOT_STARTED = "not_started"
    ACTIVE = "active"
    PAUSED = "paused"

class FeedWeekStatus:
    """Статусы недели ленты"""
    PLANNING = "planning"
    ACTIVE = "active"
    COMPLETED = "completed"

# ============= УРОВНИ СЛОЖНОСТИ =============

DIFFICULTY_LEVELS = {
    "easy": {"emoji": "🔰", "name": "Начальный", "desc": "С нуля, простым языком"},
    "medium": {"emoji": "🌿", "name": "Средний", "desc": "Есть базовые знания"},
    "hard": {"emoji": "🌳", "name": "Продвинутый", "desc": "Глубокое погружение"}
}

LEARNING_STYLES = {
    "theoretical": {"emoji": "📐", "name": "Теоретик", "desc": "Сначала теория, потом практика"},
    "practical": {"emoji": "🔧", "name": "Практик", "desc": "Учусь на примерах и задачах"},
    "mixed": {"emoji": "⚖️", "name": "Смешанный", "desc": "Баланс теории и практики"}
}

EXPERIENCE_LEVELS = {
    "student": {"emoji": "🚀", "name": "Начинающий", "desc": "Только начинаю профессиональный путь"},
    "junior": {"emoji": "🌱", "name": "Junior", "desc": "0-2 года опыта"},
    "middle": {"emoji": "💼", "name": "Middle", "desc": "2-5 лет опыта"},
    "senior": {"emoji": "⭐", "name": "Senior", "desc": "5+ лет опыта"},
    "switching": {"emoji": "🔄", "name": "Меняю сферу", "desc": "Перехожу из другой области"}
}

STUDY_DURATIONS = {
    "5": {"emoji": "⚡", "name": "5 минут", "desc": "Быстрый обзор"},
    "10": {"emoji": "🕐", "name": "10 минут", "desc": "Краткое изучение"},
    "15": {"emoji": "🕑", "name": "15 минут", "desc": "Стандартное изучение"},
    "20": {"emoji": "🕒", "name": "20 минут", "desc": "Углублённое изучение"},
    "25": {"emoji": "🕓", "name": "25 минут", "desc": "Полное погружение"}
}

# ============= CONTENT BUDGET MODEL (DP.D.027) =============
# Три оси: Длина (время × WPM) | Глубина (bloom instruction) | Персонализация (tier context)
# words = duration_minutes × WPM_BASE × BLOOM_MULTIPLIER[bloom] × DEPTH_MULTIPLIER[depth]

WPM_BASE = 60  # слов/мин — базовая скорость чтения учебного текста

BLOOM_MULTIPLIER = {1: 1.0, 2: 1.3, 3: 1.7}

# Ф-Bot-Digest-MaxTokens (WP-7, 2026-07-06): depth_level (день-к-дню прогрессия темы в
# Ленте) раньше влиял только на текст инструкции стиля, не на бюджет слов — расходилось
# с докстрингом generate_multi_topic_digest ("с каждым днём... раскрываются глубже").
DEPTH_MULTIPLIER = {1: 1.0, 2: 1.2, 3: 1.5}

BLOOM_INSTRUCTION = {
    1: "Объясни доступно, без терминов. Примеры из повседневной жизни.",
    2: "Используй профессиональную терминологию. Показывай связи между понятиями.",
    3: "Экспертный уровень. Критический анализ, неочевидные аспекты, ссылки на источники.",
}

# Output limit по модели — источник для adaptive max_tokens в generate_multi_topic_digest.
# Сверить перед сменой модели: молчаливое занижение здесь вернёт truncation обратно.
MAX_OUTPUT_TOKENS_BY_MODEL = {
    CLAUDE_MODEL_SONNET: 8192,
    CLAUDE_MODEL_HAIKU: 8192,
}


def calc_words(duration_minutes, bloom_level: int = 1, depth_level: int = 1) -> int:
    """Рассчитать целевое количество слов по Content Budget Model.

    Безопасна к str / range / None: нормализует duration_minutes перед расчётом.
    Принимает int 15, str '15', legacy range '5-10' (берёт первое число), None/'' (→ 15).
    depth_level — опционален (дефолт 1 = множитель 1.0), существующие вызовы без
    этого аргумента не меняют поведение.
    """
    if duration_minutes is None or duration_minutes == "":
        duration_int = 15
    else:
        try:
            duration_str = str(duration_minutes).split("-")[0]
            duration_int = int(duration_str)
        except (ValueError, TypeError):
            duration_int = 15

    try:
        bl_int = int(bloom_level) if bloom_level is not None else 1
    except (ValueError, TypeError):
        bl_int = 1
    bl = max(1, min(bl_int, 3))

    try:
        dl_int = int(depth_level) if depth_level is not None else 1
    except (ValueError, TypeError):
        dl_int = 1
    dl = max(1, min(dl_int, 3))

    return int(
        duration_int * WPM_BASE * BLOOM_MULTIPLIER.get(bl, 1.0) * DEPTH_MULTIPLIER.get(dl, 1.0)
    )


# Telegram Markdown v1 formatting rules for Claude prompts
TELEGRAM_MARKDOWN_RULES = (
    "ПРАВИЛА ФОРМАТИРОВАНИЯ (Telegram Markdown):\n"
    "- Используй *жирный* (одинарная звёздочка) для ключевых терминов.\n"
    "- НЕ используй ** (двойную звёздочку).\n"
    "- НЕ используй вложенное форматирование (*_текст_*).\n"
    "- ЗАПРЕЩЕНО использовать markdown-заголовки (# ## ### и т.п.) — Telegram их НЕ поддерживает, знак # показывается как обычный текст.\n"
    "- Код: `inline` или тройные обратные кавычки для блоков.\n"
    "- Ссылки: [текст](URL) — убедись что скобки закрыты.\n"
    "- Всегда закрывай форматирование: каждый * и _ должен иметь пару."
)


# ============= УРОВНИ СЛОЖНОСТИ (бывш. Bloom) =============

COMPLEXITY_LEVELS = {
    1: {
        "emoji": "🔵",
        "name": "Различения",
        "short_name": "Сложность-1",
        "desc": "Различение и запоминание понятий",
        "question_type": "В чём разница между {concept} и связанными понятиями?",
        "prompt": "Создай вопрос на РАЗЛИЧЕНИЕ понятий. Попроси объяснить, в чём разница между концепциями, чем отличаются подходы."
    },
    2: {
        "emoji": "🟡",
        "name": "Понимание",
        "short_name": "Сложность-2",
        "desc": "Открытые вопросы на понимание",
        "question_type": "Как вы понимаете {concept}? Почему это важно?",
        "prompt": "Создай ОТКРЫТЫЙ вопрос на понимание. Попроси объяснить своими словами, раскрыть связи между понятиями, объяснить почему что-то важно."
    },
    3: {
        "emoji": "🔴",
        "name": "Применение",
        "short_name": "Сложность-3",
        "desc": "Анализ и примеры из практики",
        "question_type": "Приведите пример {concept} из вашей жизни или работы. Проанализируйте ситуацию.",
        "prompt": "Создай вопрос на ПРИМЕНЕНИЕ и АНАЛИЗ. Попроси привести конкретный пример из личной жизни или рабочей практики, проанализировать ситуацию, объяснить коллеге."
    }
}

# Для обратной совместимости
BLOOM_LEVELS = COMPLEXITY_LEVELS

# Автоматическое повышение уровня: после N тем на текущем уровне
COMPLEXITY_AUTO_UPGRADE_AFTER = 7
BLOOM_AUTO_UPGRADE_AFTER = COMPLEXITY_AUTO_UPGRADE_AFTER  # для совместимости

# ============= ЛИМИТЫ МАРАФОНА =============

# Дневной лимит: 2 темы = 1 урок (теория) + 1 задание (практика)
DAILY_TOPICS_LIMIT = 2

# Максимум тем в день с учётом наверстывания (2 дня = 4 темы)
MAX_TOPICS_PER_DAY = 4

MARATHON_DAYS = 14  # длительность марафона

# ============= НАСТРОЙКИ ЛЕНТЫ =============

FEED_DAYS_PER_WEEK = 7  # checkpoint глубины (не ограничивает количество дней, continuous mode)
FEED_SESSION_DURATION_MIN = 5  # минимальная длительность сессии (мин)
FEED_SESSION_DURATION_MAX = 12  # максимальная длительность сессии (мин)
FEED_TOPICS_TO_SUGGEST = 5  # сколько тем предлагать на выбор

# ============= НАСТРОЙКИ ТРЕНИРОВКИ (WP-55) =============

ZP_PRINCIPLES = ["ZP.1", "ZP.2", "ZP.3", "ZP.4", "ZP.5", "ZP.6"]

# Детские Z-принципы (из kids-learning-pack Д. Асфандияров, Pack-source)
# Маппинг Z0-Z7 → ZP: см. DS-principles-curriculum/data/curriculum/kids_cells.json
KID_PRINCIPLES = ["Z0", "Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7"]
KID_MAX_DEPTH = 2
TRAINING_MAX_DEPTH = 5
TRAINING_COGNITIVE_LEVELS = {
    "postformal": "Взрослый",
    "formal_operational": "Подросток (11-16)",
    "concrete_operational": "Ребёнок (7-11)",
    "preoperational": "Ребёнок (3-6)",
}
TRAINING_MIN_ANSWER_LENGTH = 20
TRAINING_PASS_THRESHOLD = 0.7
TRAINING_PARTIAL_THRESHOLD = 0.4

# Пониженные пороги для первых глубин (1-2): ученик только входит в тему
TRAINING_THRESHOLDS_BY_DEPTH = {
    1: {'pass': 0.5, 'partial': 0.3},
    2: {'pass': 0.5, 'partial': 0.3},
    # 3-5: используются глобальные TRAINING_PASS/PARTIAL_THRESHOLD
}

# ============= НАСТРОЙКИ ИНТЕНТОВ =============

# Вопросительные слова для всех поддерживаемых языков
QUESTION_WORDS = [
    # Русские
    'что', 'как', 'почему', 'зачем', 'когда', 'где', 'кто', 'какой', 'можно ли', 'чем',
    # Английские
    'what', 'how', 'why', 'when', 'where', 'who', 'which', 'can', 'could', 'is', 'are', 'do', 'does',
    # Испанские
    'qué', 'cómo', 'por qué', 'cuándo', 'dónde', 'quién', 'cuál', 'puede',
]
TOPIC_REQUEST_PATTERNS = ['хочу', 'давай', 'предложи', 'тема', 'want', 'give', 'suggest', 'topic', 'quiero', 'dame']
COMMAND_WORDS = {
    # Русские
    'проще': 'simpler',
    'глубже': 'deeper',
    'примеры': 'examples',
    'дальше': 'next',
    'пропустить': 'skip',
    # Английские
    'simpler': 'simpler',
    'deeper': 'deeper',
    'examples': 'examples',
    'next': 'next',
    'skip': 'skip',
    # Испанские
    'más simple': 'simpler',
    'más profundo': 'deeper',
    'ejemplos': 'examples',
    'siguiente': 'next',
    'saltar': 'skip',
}

# ============= ОНТОЛОГИЧЕСКИЕ ПРАВИЛА =============
# Эти правила имеют приоритет над разговорными формулировками.
# Claude должен автоматически исправлять нарушения.

ONTOLOGY_RULES = """
ОНТОЛОГИЧЕСКИЙ КОНТРОЛЬ (обязателен для всего контента):

1) СИСТЕМА — это вещь (объект с элементами и связями), а НЕ процесс, метод или абстракция.
   ЗАПРЕЩЕНО: «система привычек», «система мышления», «система обучения», «система развития», «система целеполагания», «система знаний».
   КОРРЕКТНО: набор практик, метод обучения, процесс развития, подход к целеполаганию, набор описаний (или экзокортекс, если речь о конкретной системе).

2) РАБОЧИЙ ПРОДУКТ — материально зафиксированный артефакт (можно увидеть, передать, хранить).
   Формулируется СУЩЕСТВИТЕЛЬНЫМ, обозначающим ДОКУМЕНТ, ВЕЩЬ или СИСТЕМУ В ОПРЕДЕЛЁННОМ СОСТОЯНИИ.
   ЗАПРЕЩЕНО как РП: «сделать», «внедрить», «улучшить», «повысить».
   ЗАПРЕЩЕНО начинать с отглагольных существительных-процессов: «анализ», «исследование», «обзор», «диагностика», «сравнение».
   КОРРЕКТНО: чек-лист, схема, текст поста, таблица, список, описание, план, реестр, набор правил.
   Тест: можно ли это распечатать и передать другому человеку?

3) ЦЕЛЬ — изменение состояния системы, а НЕ средство и НЕ действие.
   ИИ, CRM, методологии — это СРЕДСТВА, не цели.
   ЗАПРЕЩЕНО: «внедрить ИИ» как цель.
   КОРРЕКТНО: «сократить время на X» (цель), «используя ИИ» (средство).

4) ФУНКЦИЯ ≠ ЭЛЕМЕНТ.
   Функция — «что делает система». Элемент — «чем/кем делается».
   ЗАПРЕЩЕНО: отождествлять функцию с человеком или отделом.

5) РОЛЬ ≠ ЧЕЛОВЕК.
   Роль — функциональная позиция (за ней метод и РП). Человек — носитель ролей.
   ЗАПРЕЩЕНО: «Петя = менеджер» (Петя исполняет роль менеджера).

6) ПРОБЛЕМА ≠ СИМПТОМ.
   «Не хватает времени» — симптом. Проблема — структурное несоответствие.
   ЗАПРЕЩЕНО: называть симптомы проблемами.

7) СОСТОЯНИЕ ≠ ПРОЦЕСС.
   Обучение, развитие — процессы. Их результаты — состояния или артефакты.

8) ЛЕКСИКА — РАЗВИТИЕ, НЕ ОБРАЗОВАНИЕ (WP-478, DP.D.132/D.182).
   Пользователь — участник или стажёр, НЕ студент, ученик, первокурсник.
   Единица активности — занятие, НЕ урок. Процесс — развитие, НЕ обучение.
   Платформа — рабочая среда, НЕ учебная платформа.
   Допустимо: доменные термины («методы обучения», «теория обучения», «инженерия обучения»)
   в предметном контенте о саморазвитии.
"""

# Краткая версия для генерации тем (только ключевое правило про "система")
ONTOLOGY_RULES_TOPICS = """
ТЕРМИНОЛОГИЧЕСКИЕ ЗАПРЕТЫ для названий тем:
- Слово «система» допустимо ТОЛЬКО для физических/социальных объектов с элементами и связями
- ЗАПРЕЩЕНО: «система обучения», «система развития», «система привычек», «система мышления», «система целеполагания», «система знаний»
- КОРРЕКТНО: «метод обучения», «процесс развития», «набор практик», «подход к мышлению», «целеполагание», «набор описаний» / «экзокортекс»

Примеры исправлений:
- ❌ "Личная система обучения" → ✅ "Методы эффективного обучения"
- ❌ "Система целеполагания" → ✅ "Практики целеполагания"
- ❌ "Система привычек" → ✅ "Формирование привычек"
- ❌ "Система знаний" → ✅ "Экзокортекс" или "Набор описаний предметных областей"
"""

# ============= КАТЕГОРИИ РАБОЧИХ ПРОДУКТОВ =============

# ============= ПОДПИСКА (Aisystant «Инженерия интеллекта») =============

# Заблокированные сервисы (без подписки БР)
LOCKED_SERVICES = {"feed", "consultation", "notes", "plans"}

# ============= ПЛАТФОРМА (DP.ARCH.002 § 12.9) =============

PLATFORM_URLS = {
    "site": "https://system-school.ru/",
    "subscription": "https://system-school.ru/open-endedness",
    "schedule": "https://system-school.ru/list",
    "lr": "https://system-school.ru/programs/intro",
    "rr": "https://system-school.ru/programs/orgdev",
    "ir": "https://system-school.ru/programs/research",
    "guides": "https://docs.system-school.ru/ru/",
}

# ============= WAKATIME (WP-60) =============
WAKATIME_API_KEY = os.getenv("WAKATIME_API_KEY")

# ============= CHATWOOT (helpdesk, WP-341) =============

CHATWOOT_URL = os.getenv("CHATWOOT_URL", "")
CHATWOOT_INBOX_IDENTIFIER = os.getenv("CHATWOOT_INBOX_IDENTIFIER", "")
CHATWOOT_WEBHOOK_SECRET = os.getenv("CHATWOOT_WEBHOOK_SECRET", "")

# ============= DISCOURSE (systemsworld.club) =============

DISCOURSE_API_URL = os.getenv("DISCOURSE_API_URL", "")
DISCOURSE_API_KEY = os.getenv("DISCOURSE_API_KEY", "")
DISCOURSE_BLOGS_CATEGORY_ID = int(os.getenv("DISCOURSE_BLOGS_CATEGORY_ID", "36"))

# ============= PUBLISHER (R21, WP-53 Phase 3) =============

GITHUB_TOKEN = GITHUB_BOT_PAT or ""  # L2 Auto-Fix PAT (org repo only)
# GITHUB_KNOWLEDGE_REPO removed — Publisher uses per-user OAuth tokens via github_connections.knowledge_repo
PUBLISHER_DAYS = os.getenv("PUBLISHER_DAYS", "mon,tue,wed,thu,fri,sat,sun")  # Дни публикации (ежедневно)
PUBLISHER_TIME = os.getenv("PUBLISHER_TIME", "10:00")  # Время публикации (МСК)
PUBLISHER_INTERVAL = int(os.getenv("PUBLISHER_INTERVAL", "2"))  # Мин. интервал между публикациями (дней)
PUBLISHER_MIN_QUEUE = int(os.getenv("PUBLISHER_MIN_QUEUE", "2"))  # Мин. очередь

# ============= EVALUATOR (DS-evaluator-agent) =============

# Включить проверку ответов (Claude Haiku ~1-2 сек на оценку)
EVALUATION_ENABLED = os.getenv("EVALUATION_ENABLED", "true").lower() == "true"

# Включить валидацию формулировки РП
WP_VALIDATION_ENABLED = os.getenv("WP_VALIDATION_ENABLED", "true").lower() == "true"

# Включить запись фиксаций в fleeting-notes (для GitHub-пользователей)
FIXATION_ENABLED = os.getenv("FIXATION_ENABLED", "true").lower() == "true"

# ============= EXTERNAL SESSION /claude (WP-358) =============

# Marathon/Assessment стейты, в которых SM ждёт ответа пилота. Если пилот
# в одном из этих стейтов И last user_state.updated_at свежее лимита (в минутах) —
# свободный текст уходит в fallback → SM, не в активную /claude сессию.
# Mutex между marathon SM (development.user_state.current_state) и aiogram FSM
# (ExternalSession.active) — явный, не «бесплатный»: разные state machines.
SM_EXPECTING_REPLY_STATES: dict[str, int] = {
    "workshop.marathon.question": 60,    # ждём ответ на вопрос ≤60 мин
    "workshop.marathon.task": 1440,      # задание может выполняться до 24ч
    "workshop.marathon.bonus": 60,
    "workshop.assessment.flow": 60,
}

# ============= КАТЕГОРИИ РАБОЧИХ ПРОДУКТОВ =============

WORK_PRODUCT_CATEGORIES = {
    'diagnosis': 'диагностика',
    'tracker': 'трекер',
    'hypothesis': 'гипотеза',
    'checklist': 'чек-лист',
    'meme_transform': 'трансформация мемов',
    'schema': 'схема',
    'plan': 'план',
    'fixation': 'фиксация',
}
