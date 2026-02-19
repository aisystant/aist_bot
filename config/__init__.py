"""
Модуль конфигурации бота.

Содержит:
- settings.py: все константы, токены, настройки
"""

from .settings import (
    # Токены
    BOT_TOKEN,
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL_SONNET,
    CLAUDE_MODEL_HAIKU,
    DATABASE_URL,
    KNOWLEDGE_MCP_URL,
    DIGITAL_TWIN_MCP_URL,
    validate_env,

    # Linear OAuth (тестовая интеграция)
    LINEAR_CLIENT_ID,
    LINEAR_CLIENT_SECRET,
    LINEAR_REDIRECT_URI,
    OAUTH_SERVER_PORT,

    # GitHub OAuth
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_REDIRECT_URI,

    # Feature flags
    USE_STATE_MACHINE,

    # Логирование
    get_logger,

    # Временная зона
    MOSCOW_TZ,

    # Пути
    BASE_DIR,
    TOPICS_DIR,
    KNOWLEDGE_STRUCTURE_PATH,

    # Режимы и статусы
    Mode,
    MarathonStatus,
    FeedStatus,
    FeedWeekStatus,

    # Настройки пользователя
    DIFFICULTY_LEVELS,
    LEARNING_STYLES,
    EXPERIENCE_LEVELS,
    STUDY_DURATIONS,

    # Content Budget Model (DP.D.027)
    WPM_BASE,
    BLOOM_MULTIPLIER,
    BLOOM_INSTRUCTION,
    calc_words,

    # Сложность (бывш. Bloom)
    COMPLEXITY_LEVELS,
    BLOOM_LEVELS,
    COMPLEXITY_AUTO_UPGRADE_AFTER,
    BLOOM_AUTO_UPGRADE_AFTER,

    # Лимиты
    DAILY_TOPICS_LIMIT,
    MAX_TOPICS_PER_DAY,
    MARATHON_DAYS,

    # Лента
    FEED_DAYS_PER_WEEK,
    FEED_SESSION_DURATION_MIN,
    FEED_SESSION_DURATION_MAX,
    FEED_TOPICS_TO_SUGGEST,

    # Интенты
    QUESTION_WORDS,
    TOPIC_REQUEST_PATTERNS,
    COMMAND_WORDS,

    # Категории РП
    WORK_PRODUCT_CATEGORIES,

    # Онтологические правила
    ONTOLOGY_RULES,
    ONTOLOGY_RULES_TOPICS,
)

__all__ = [
    'BOT_TOKEN',
    'ANTHROPIC_API_KEY',
    'DATABASE_URL',
    'KNOWLEDGE_MCP_URL',
    'DIGITAL_TWIN_MCP_URL',
    'validate_env',
    'LINEAR_CLIENT_ID',
    'LINEAR_CLIENT_SECRET',
    'LINEAR_REDIRECT_URI',
    'OAUTH_SERVER_PORT',
    'GITHUB_CLIENT_ID',
    'GITHUB_CLIENT_SECRET',
    'GITHUB_REDIRECT_URI',
    'USE_STATE_MACHINE',
    'get_logger',
    'MOSCOW_TZ',
    'BASE_DIR',
    'TOPICS_DIR',
    'KNOWLEDGE_STRUCTURE_PATH',
    'Mode',
    'MarathonStatus',
    'FeedStatus',
    'FeedWeekStatus',
    'DIFFICULTY_LEVELS',
    'LEARNING_STYLES',
    'EXPERIENCE_LEVELS',
    'STUDY_DURATIONS',
    'WPM_BASE',
    'BLOOM_MULTIPLIER',
    'BLOOM_INSTRUCTION',
    'calc_words',
    'COMPLEXITY_LEVELS',
    'BLOOM_LEVELS',
    'COMPLEXITY_AUTO_UPGRADE_AFTER',
    'BLOOM_AUTO_UPGRADE_AFTER',
    'DAILY_TOPICS_LIMIT',
    'MAX_TOPICS_PER_DAY',
    'MARATHON_DAYS',
    'FEED_DAYS_PER_WEEK',
    'FEED_SESSION_DURATION_MIN',
    'FEED_SESSION_DURATION_MAX',
    'FEED_TOPICS_TO_SUGGEST',
    'QUESTION_WORDS',
    'TOPIC_REQUEST_PATTERNS',
    'COMMAND_WORDS',
    'WORK_PRODUCT_CATEGORIES',
    'ONTOLOGY_RULES',
    'ONTOLOGY_RULES_TOPICS',
]
