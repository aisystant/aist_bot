"""
Модуль для работы со структурой знаний марафона.

Загружает knowledge_structure.yaml и предоставляет функции
для работы с темами.
"""

from typing import Optional, List, Tuple
from datetime import datetime
import yaml
from pathlib import Path

from config import get_logger, KNOWLEDGE_STRUCTURE_PATH, MARATHON_DAYS

logger = get_logger(__name__)

# Глобальные переменные для хранения загруженных данных
_TOPICS: List[dict] = []
_MARATHON_META: dict = {}
_loaded = False


def load_knowledge_structure() -> Tuple[List[dict], dict]:
    """Загружает структуру знаний из YAML файла.

    Returns:
        (topics, meta) - список тем и метаданные марафона
    """
    global _TOPICS, _MARATHON_META, _loaded

    if _loaded:
        return _TOPICS, _MARATHON_META

    try:
        with open(KNOWLEDGE_STRUCTURE_PATH, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        meta = data.get('meta', {})

        # Темы находятся в отдельном ключе 'topics', а не внутри sections
        # Каждая тема уже содержит поле 'day'
        topics = data.get('topics', [])

        # Добавляем section_id к каждой теме на основе дня
        sections = data.get('sections', [])
        day_to_section = {}
        for section in sections:
            section_id = section.get('id', 'unknown')
            for day in section.get('days', []):
                day_to_section[day] = section_id

        for topic in topics:
            day = topic.get('day', 1)
            topic['section'] = day_to_section.get(day, 'unknown')

        _TOPICS = topics
        _MARATHON_META = meta
        _loaded = True

        logger.info(f"Загружено {len(topics)} тем марафона ({meta.get('total_days', 14)} дней)")
        return topics, meta

    except Exception as e:
        logger.error(f"Ошибка загрузки knowledge_structure.yaml: {e}")
        _TOPICS = []
        _MARATHON_META = {}
        _loaded = True
        return [], {}


def get_topic(index: int) -> Optional[dict]:
    """Получить тему по индексу."""
    if not _loaded:
        load_knowledge_structure()
    return _TOPICS[index] if index < len(_TOPICS) else None


def get_topic_by_index(index: int) -> Optional[dict]:
    """Алиас для get_topic."""
    return get_topic(index)


def get_topic_title(topic: dict, lang: str = 'ru') -> str:
    """Получить название темы на нужном языке.

    Ищет title_{lang} в topic, если нет - возвращает title (русский).
    """
    if lang != 'ru':
        localized_key = f'title_{lang}'
        if localized_key in topic:
            return topic[localized_key]
    return topic.get('title', '')


def get_total_topics() -> int:
    """Получить общее количество тем."""
    if not _loaded:
        load_knowledge_structure()
    return len(_TOPICS)


def get_topics_for_day(day: int) -> List[dict]:
    """Получить темы для конкретного дня марафона."""
    if not _loaded:
        load_knowledge_structure()
    return [t for t in _TOPICS if t.get('day') == day]


def get_marathon_day_from_progress(completed_topics: list, current_topic_index: int = 0) -> int:
    """Вычислить день марафона по прогрессу.

    Args:
        completed_topics: список индексов завершённых тем
        current_topic_index: текущий индекс темы

    Returns:
        Номер дня марафона (1-14)
    """
    # По количеству завершённых тем
    # 2 темы в день (урок + задание)
    return len(completed_topics) // 2 + 1


def get_available_topics(completed_topics: list, marathon_day: int, topics_today: int, max_per_day: int = 4) -> List[Tuple[int, dict]]:
    """Получить доступные темы с учётом правил марафона.

    Args:
        completed_topics: список индексов завершённых тем
        marathon_day: текущий день марафона
        topics_today: сколько тем уже пройдено сегодня
        max_per_day: максимум тем в день

    Returns:
        Список кортежей (index, topic)
    """
    if not _loaded:
        load_knowledge_structure()

    if topics_today >= max_per_day:
        return []

    completed = set(completed_topics)
    available = []

    for i, topic in enumerate(_TOPICS):
        if i in completed:
            continue
        if topic.get('day', 1) > marathon_day:
            continue
        available.append((i, topic))

    return available


def get_next_topic_index(completed_topics: list, marathon_day: int, topics_today: int, max_per_day: int = 4) -> Optional[int]:
    """Получить индекс следующей темы для изучения.

    Returns:
        Индекс темы или None если нет доступных
    """
    available = get_available_topics(completed_topics, marathon_day, topics_today, max_per_day)
    if available:
        return available[0][0]
    return None
