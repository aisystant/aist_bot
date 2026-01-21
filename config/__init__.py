"""
Модуль конфигурации бота.

Содержит:
- settings.py: все константы, токены, настройки
"""

from .settings import (
    # Токены
    BOT_TOKEN,
    ANTHROPIC_API_KEY,
    DATABASE_URL,
    MCP_URL,
    KNOWLEDGE_MCP_URL,
    validate_env,

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
    'MCP_URL',
    'KNOWLEDGE_MCP_URL',
    'validate_env',
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
