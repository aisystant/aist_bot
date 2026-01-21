"""
Вспомогательные функции для генерации контента.

Содержит:
- load_topic_metadata: загрузка метаданных темы из YAML
- get_search_keys: получение ключей поиска для MCP
- get_bloom_questions: настройки вопросов по уровню Блума
- get_personalization_prompt: промпт для персонализации контента
- get_example_rules: правила для примеров с ротацией по дню марафона
"""

from pathlib import Path
from typing import Optional, List
import yaml

from config import get_logger, STUDY_DURATIONS, TOPICS_DIR

logger = get_logger(__name__)

# ============= КОНСТАНТЫ ДЛЯ РОТАЦИИ ПРИМЕРОВ =============

# Шаблоны форматов примеров для ротации
EXAMPLE_TEMPLATES = [
    ("аналогия", "Используй аналогию — перенеси структуру или принцип из одной области в другую"),
    ("мини-кейс", "Используй мини-кейс — опиши ситуацию → выбор → последствия"),
    ("контрпример", "Используй контрпример — покажи как НЕ работает, чтобы подчеркнуть как работает правильно"),
    ("сравнение", "Используй сравнение двух подходов — правильный vs неправильный"),
    ("ошибка-мастерство", "Покажи типичную ошибку новичка и приём мастера"),
    ("наблюдение", "Предложи наблюдательный эксперимент — что можно заметить в повседневной жизни"),
]

# Источники примеров для ротации
EXAMPLE_SOURCES = ["работа", "близкая профессиональная сфера", "интерес/хобби", "далёкая сфера для контраста"]


def load_topic_metadata(topic_id: str) -> Optional[dict]:
    """Загружает метаданные темы из YAML файла

    Args:
        topic_id: ID темы (например, "1-1-three-states")

    Returns:
        Словарь с метаданными или None если файл не найден
    """
    if not TOPICS_DIR.exists():
        return None

    # Пробуем найти файл по ID
    for yaml_file in TOPICS_DIR.glob("*.yaml"):
        if yaml_file.name.startswith("_"):  # Пропускаем служебные файлы
            continue
        try:
            with open(yaml_file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                if data and data.get('id') == topic_id:
                    return data
        except Exception as e:
            logger.error(f"Ошибка загрузки метаданных {yaml_file}: {e}")

    return None


def get_bloom_questions(metadata: dict, bloom_level: int, study_duration: int) -> dict:
    """Получает настройки вопросов для заданного уровня Блума и времени

    Args:
        metadata: метаданные темы
        bloom_level: уровень Блума (1, 2 или 3)
        study_duration: время на тему в минутах (5, 10, 15, 20, 25)

    Returns:
        Словарь с настройками вопросов или пустой словарь
    """
    time_levels = metadata.get('time_levels', {})

    # Нормализуем время к ближайшему уровню (5, 15, 25)
    if study_duration <= 5:
        time_key = 5
    elif study_duration <= 15:
        time_key = 15
    else:
        time_key = 25

    time_config = time_levels.get(time_key, {})
    bloom_key = f"bloom_{bloom_level}"

    return time_config.get(bloom_key, {})


def get_search_keys(metadata: dict, mcp_type: str = "guides_mcp") -> List[str]:
    """Получает ключи поиска для MCP из метаданных

    Args:
        metadata: метаданные темы
        mcp_type: тип MCP ("guides_mcp" или "knowledge_mcp")

    Returns:
        Список поисковых запросов
    """
    search_keys = metadata.get('search_keys', {})
    return search_keys.get(mcp_type, [])


def get_example_rules(intern: dict, marathon_day: int) -> str:
    """Генерирует правила для примеров с ротацией по дню марафона.

    Каждый день марафона использует разные:
    - Форматы примеров (аналогия, мини-кейс, контрпример и т.д.)
    - Порядок источников (работа, хобби, далёкая сфера)
    - Интерес из списка интересов стажера

    Args:
        intern: профиль стажера
        marathon_day: день марафона (1-14)

    Returns:
        Строка с правилами для примеров
    """
    interests = intern.get('interests', [])
    occupation = intern.get('occupation', '') or 'работа'

    # Выбираем интерес по дню (циклически)
    if interests:
        interest_idx = (marathon_day - 1) % len(interests)
        today_interest = interests[interest_idx]
        other_interests = [i for idx, i in enumerate(interests) if idx != interest_idx]
    else:
        today_interest = None
        other_interests = []

    # Выбираем шаблон формата по дню
    template_idx = (marathon_day - 1) % len(EXAMPLE_TEMPLATES)
    template_name, template_instruction = EXAMPLE_TEMPLATES[template_idx]

    # Ротация порядка источников по дню
    shift = (marathon_day - 1) % len(EXAMPLE_SOURCES)
    rotated_sources = EXAMPLE_SOURCES[shift:] + EXAMPLE_SOURCES[:shift]

    # Формируем правила
    sources_text = "\n".join([f"  {i+1}. {src}" for i, src in enumerate(rotated_sources)])

    interest_text = f'"{today_interest}"' if today_interest else "не указан"
    other_interests_text = f" (другие интересы для разнообразия: {', '.join(other_interests)})" if other_interests else ""

    return f"""
ПРАВИЛА ДЛЯ ПРИМЕРОВ (День {marathon_day}):

Формат примеров сегодня: **{template_name}**
{template_instruction}

Порядок источников для примеров (от первого к последнему):
{sources_text}

Детали источников:
- Работа/профессия: "{occupation}"
- Интерес дня: {interest_text}{other_interests_text}
- Близкая сфера: смежная с работой "{occupation}" область
- Далёкая сфера: что-то неожиданное для контраста (спорт, искусство, природа, история)

ВАЖНО: Используй интерес дня ({interest_text}), а НЕ всегда первый из списка!
"""


def get_personalization_prompt(intern: dict, marathon_day: int = 1) -> str:
    """Генерирует промпт для персонализации на основе профиля стажера.

    Args:
        intern: словарь с профилем стажера
        marathon_day: день марафона для ротации примеров (по умолчанию 1)

    Returns:
        Строка с инструкциями для персонализации
    """
    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {"words": 1500})

    interests = ', '.join(intern['interests']) if intern['interests'] else 'не указаны'
    occupation = intern.get('occupation', '') or 'не указано'
    motivation = intern.get('motivation', '') or 'не указано'
    goals = intern.get('goals', '') or 'не указаны'

    # Получаем правила для примеров с ротацией по дню
    example_rules = get_example_rules(intern, marathon_day)

    return f"""
ПРОФИЛЬ СТАЖЕРА:
- Имя: {intern['name']}
- Занятие: {occupation}
- Интересы/хобби: {interests}
- Что важно в жизни: {motivation}
- Что хочет изменить: {goals}
- Время на изучение: {intern['study_duration']} минут (~{duration.get('words', 1500)} слов)

ИНСТРУКЦИИ ПО ПЕРСОНАЛИЗАЦИИ:
1. Показывай, как тема помогает достичь того, что стажер хочет изменить: "{goals}"
2. Добавляй мотивационный блок, опираясь на ценности стажера: "{motivation}"
3. Объём текста должен быть рассчитан на {intern['study_duration']} минут чтения (~{duration.get('words', 1500)} слов)
4. Пиши простым языком, избегай академического стиля
{example_rules}"""
