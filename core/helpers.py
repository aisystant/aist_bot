"""
Вспомогательные функции для генерации контента и роутинга.

Содержит:
- get_user_mode_state: определение целевого стейта по режиму пользователя
- load_topic_metadata: загрузка метаданных темы из YAML
- get_search_keys: получение ключей поиска для MCP
- get_bloom_questions: настройки вопросов по уровню Блума
- get_personalization_prompt: промпт для персонализации контента
"""

from pathlib import Path
from typing import Optional, List
import yaml

from config import get_logger, STUDY_DURATIONS, TOPICS_DIR

logger = get_logger(__name__)


# ============= РОУТИНГ ПО РЕЖИМУ =============

# Маппинг mode → стейт для /learn и scheduler
MODE_STATE_MAP = {
    'marathon': 'workshop.marathon.lesson',
    'feed': 'feed.digest',
    'both': 'workshop.marathon.lesson',  # /learn → марафон при обоих режимах
}


def get_user_mode_state(user) -> str:
    """Определить целевой стейт обучения по режиму пользователя.

    Args:
        user: dict или объект пользователя из БД

    Returns:
        Имя стейта (например, "workshop.marathon.lesson" или "feed.digest")
    """
    if isinstance(user, dict):
        mode = user.get('mode', 'marathon')
    else:
        mode = getattr(user, 'mode', 'marathon')

    return MODE_STATE_MAP.get(mode or 'marathon', MODE_STATE_MAP['marathon'])


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


def get_personalization_prompt(intern: dict) -> str:
    """Генерирует промпт для персонализации на основе профиля стажера

    Args:
        intern: словарь с профилем стажера

    Returns:
        Строка с инструкциями для персонализации
    """
    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {"words": 1500})

    interests = ', '.join(intern['interests']) if intern['interests'] else 'не указаны'
    occupation = intern.get('occupation', '') or 'не указано'
    motivation = intern.get('motivation', '') or 'не указано'
    goals = intern.get('goals', '') or 'не указаны'

    # Адаптация по состоянию из теста систематичности
    assessment_block = _get_assessment_adaptation(intern)

    return f"""
ПРОФИЛЬ СТАЖЕРА:
- Имя: {intern['name']}
- Занятие: {occupation}
- Интересы/хобби: {interests}
- Что важно в жизни: {motivation}
- Что хочет изменить: {goals}
- Время на изучение: {intern['study_duration']} минут (~{duration.get('words', 1500)} слов)
{assessment_block}
ИНСТРУКЦИИ ПО ПЕРСОНАЛИЗАЦИИ:
1. Показывай, как тема помогает достичь того, что стажер хочет изменить: "{goals}"
2. Добавляй мотивационный блок, опираясь на ценности стажера: "{motivation}"
3. Объём текста должен быть рассчитан на {intern['study_duration']} минут чтения (~{duration.get('words', 1500)} слов)
4. Пиши простым языком, избегай академического стиля

ПРАВИЛА ДЛЯ ПРИМЕРОВ:
- Первый пример — из рабочей сферы стажера ("{occupation}")
- Второй пример — из близкой профессиональной сферы
- Третий пример (если нужен) — из интересов/хобби ({interests}), НЕ БОЛЕЕ ОДНОГО примера из интересов
- Четвёртый пример (если нужен) — из абсолютно далёкой сферы для контраста
"""


# Маппинг состояния теста → уровень сложности (Блум)
ASSESSMENT_BLOOM_MAP = {
    'chaos': 1,
    'deadlock': 2,
    'turning_point': 3,
}

# Адаптации контента по состоянию
_ASSESSMENT_ADAPTATIONS = {
    'chaos': {
        'label': 'Хаос',
        'style': (
            "Стажер в состоянии хаоса: внимание фрагментировано, день уходит на тушение пожаров.\n"
            "АДАПТАЦИЯ СТИЛЯ:\n"
            "- Короткие абзацы, одна мысль за раз\n"
            "- Конкретные микро-действия (≤5 мин), которые можно сделать прямо сейчас\n"
            "- Не перегружай: лучше одна идея, усвоенная до конца, чем пять поверхностно\n"
            "- Практика = одно простое действие с немедленным результатом"
        ),
    },
    'deadlock': {
        'label': 'Тупик',
        'style': (
            "Стажер в состоянии тупика: стабильность есть, но роста нет — «день сурка».\n"
            "АДАПТАЦИЯ СТИЛЯ:\n"
            "- Акцент на применение: «ты уже знаешь это — вопрос, почему не работает?»\n"
            "- Провокационные вопросы, разрывающие петлю знание→бездействие\n"
            "- Практика = deliberate practice, конкретное применение к своей ситуации\n"
            "- Показывай разницу между «знать» и «делать» на примерах из его сферы"
        ),
    },
    'turning_point': {
        'label': 'Поворот',
        'style': (
            "Стажер в состоянии поворота: всё хорошо, но хочется большего, направление неясно.\n"
            "АДАПТАЦИЯ СТИЛЯ:\n"
            "- Рефлексивные вопросы: «как это меняет твой выбор?», «куда это ведёт?»\n"
            "- Связи между темой и жизненным направлением стажера\n"
            "- Практика = безопасный эксперимент + дневник наблюдений\n"
            "- Помогай исследовать направления, не давя на конкретный выбор"
        ),
    },
}


def _get_assessment_adaptation(intern: dict) -> str:
    """Возвращает блок адаптации по состоянию из теста систематичности."""
    state = intern.get('assessment_state')
    if not state or state not in _ASSESSMENT_ADAPTATIONS:
        return ""
    adaptation = _ASSESSMENT_ADAPTATIONS[state]
    return f"\nСОСТОЯНИЕ СТАЖЕРА (по тесту систематичности): {adaptation['label']}\n{adaptation['style']}\n"
