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

# Claude models: Sonnet для сложных задач, Haiku для простых (быстрее + дешевле)
CLAUDE_MODEL_SONNET = "claude-sonnet-4-20250514"
CLAUDE_MODEL_HAIKU = "claude-haiku-4-5-20251001"
DATABASE_URL = os.getenv("DATABASE_URL")
KNOWLEDGE_MCP_URL = os.getenv("KNOWLEDGE_MCP_URL", "https://knowledge-mcp.aisystant.workers.dev/mcp")
DIGITAL_TWIN_MCP_URL = os.getenv("DIGITAL_TWIN_MCP_URL", "https://digital-twin-mcp.aisystant.workers.dev/mcp")

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

# ============= GITHUB OAUTH =============
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI", "https://aistmebot-production.up.railway.app/auth/github/callback")

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
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не установлен!")

# ============= FEATURE FLAGS =============

# State Machine: включает новую архитектуру
# Когда True — используется StateMachine вместо старых хэндлеров
# По умолчанию False для обратной совместимости
USE_STATE_MACHINE = os.getenv("USE_STATE_MACHINE", "true").lower() == "true"

# Maintenance mode: блокирует всех кроме ALLOWED_TESTERS
# Используется для тестовых ботов, чтобы пользователи не пользовались ими как основными
MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"
MAINTENANCE_REDIRECT_BOT = os.getenv("MAINTENANCE_REDIRECT_BOT", "@aist_me_bot")

# Список разрешённых chat_id через запятую: "123456,789012"
_allowed = os.getenv("ALLOWED_TESTERS", "")
ALLOWED_TESTERS: set[int] = {int(x.strip()) for x in _allowed.split(",") if x.strip().isdigit()}

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

# ============= РЕЖИМЫ РАБОТЫ =============

class Mode:
    """Режимы работы бота"""
    MARATHON = "marathon"
    FEED = "feed"
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
    "easy": {"emoji": "🌱", "name": "Начальный", "desc": "С нуля, простым языком"},
    "medium": {"emoji": "🌿", "name": "Средний", "desc": "Есть базовые знания"},
    "hard": {"emoji": "🌳", "name": "Продвинутый", "desc": "Глубокое погружение"}
}

LEARNING_STYLES = {
    "theoretical": {"emoji": "📚", "name": "Теоретик", "desc": "Сначала теория, потом практика"},
    "practical": {"emoji": "🔧", "name": "Практик", "desc": "Учусь на примерах и задачах"},
    "mixed": {"emoji": "⚖️", "name": "Смешанный", "desc": "Баланс теории и практики"}
}

EXPERIENCE_LEVELS = {
    "student": {"emoji": "🎓", "name": "Студент", "desc": "Учусь или недавно закончил"},
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
# words = duration_minutes × WPM_BASE × BLOOM_MULTIPLIER[bloom]

WPM_BASE = 60  # слов/мин — базовая скорость чтения учебного текста

BLOOM_MULTIPLIER = {1: 1.0, 2: 1.3, 3: 1.7}

BLOOM_INSTRUCTION = {
    1: "Объясни доступно, без терминов. Примеры из повседневной жизни.",
    2: "Используй профессиональную терминологию. Показывай связи между понятиями.",
    3: "Экспертный уровень. Критический анализ, неочевидные аспекты, ссылки на источники.",
}


def calc_words(duration_minutes: int, bloom_level: int = 1) -> int:
    """Рассчитать целевое количество слов по Content Budget Model."""
    bl = max(1, min(bloom_level, 3))
    return int(duration_minutes * WPM_BASE * BLOOM_MULTIPLIER.get(bl, 1.0))


# Telegram Markdown v1 formatting rules for Claude prompts
TELEGRAM_MARKDOWN_RULES = (
    "ПРАВИЛА ФОРМАТИРОВАНИЯ (Telegram Markdown):\n"
    "- Используй *жирный* (одинарная звёздочка) для ключевых терминов.\n"
    "- НЕ используй ** (двойную звёздочку).\n"
    "- НЕ используй вложенное форматирование (*_текст_*).\n"
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

# ============= ПОДПИСКА (Stars Subscription) =============

from datetime import date as _date

# Дата запуска подписки (week 0)
SUBSCRIPTION_LAUNCH_DATE = _date(2026, 3, 1)

# Ценообразование
SUBSCRIPTION_BASE_PRICE = 50       # Stars на старте
SUBSCRIPTION_LINEAR_INCREMENT = 5  # +Stars/нед в первый месяц
SUBSCRIPTION_LINEAR_WEEKS = 4      # недель линейного роста
SUBSCRIPTION_WEEKLY_MULTIPLIER = 1.05  # множитель после линейной фазы
MAX_SUBSCRIPTION_PRICE = 500       # ~1000 руб/мес (бизнес-cap)

# Триал
FREE_TRIAL_DAYS = 15

# Заблокированные сервисы (без подписки/триала)
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

# ============= DISCOURSE (systemsworld.club) =============

DISCOURSE_API_URL = os.getenv("DISCOURSE_API_URL", "")
DISCOURSE_API_KEY = os.getenv("DISCOURSE_API_KEY", "")
DISCOURSE_BLOGS_CATEGORY_ID = int(os.getenv("DISCOURSE_BLOGS_CATEGORY_ID", "36"))

# ============= PUBLISHER (R21, WP-53 Phase 3) =============

GITHUB_TOKEN = GITHUB_BOT_PAT or ""  # reuse L2 Auto-Fix PAT
GITHUB_KNOWLEDGE_REPO = os.getenv("GITHUB_KNOWLEDGE_REPO", "")  # "owner/repo"
PUBLISHER_DAYS = os.getenv("PUBLISHER_DAYS", "mon,tue,wed,thu,fri,sat,sun")  # Дни публикации (ежедневно)
PUBLISHER_TIME = os.getenv("PUBLISHER_TIME", "10:00")  # Время публикации (МСК)
PUBLISHER_MIN_QUEUE = int(os.getenv("PUBLISHER_MIN_QUEUE", "2"))  # Мин. очередь

# ============= EVALUATOR (DS-evaluator-agent) =============

# Включить проверку ответов (Claude Haiku ~1-2 сек на оценку)
EVALUATION_ENABLED = os.getenv("EVALUATION_ENABLED", "true").lower() == "true"

# Включить валидацию формулировки РП
WP_VALIDATION_ENABLED = os.getenv("WP_VALIDATION_ENABLED", "true").lower() == "true"

# Включить запись фиксаций в fleeting-notes (для GitHub-пользователей)
FIXATION_ENABLED = os.getenv("FIXATION_ENABLED", "true").lower() == "true"

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
