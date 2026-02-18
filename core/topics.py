"""
Доменная логика работы с темами марафона.

Все функции для работы с TOPICS, прогрессом, днями марафона.
Извлечено из bot.py для чистоты архитектуры.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import yaml

from config import MARATHON_DAYS, MAX_TOPICS_PER_DAY, STUDY_DURATIONS
from db.queries import get_topics_today
from db.queries.users import moscow_today

logger = logging.getLogger(__name__)


# ============= ЗАГРУЗКА МЕТАДАННЫХ ТЕМ =============

TOPICS_DIR = Path(__file__).parent.parent / "topics"


def load_topic_metadata(topic_id: str) -> Optional[dict]:
    """Загружает метаданные темы из YAML файла"""
    if not TOPICS_DIR.exists():
        return None

    for yaml_file in TOPICS_DIR.glob("*.yaml"):
        if yaml_file.name.startswith("_"):
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
    """Получает настройки вопросов для заданного уровня Блума и времени"""
    time_levels = metadata.get('time_levels', {})

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
    """Получает ключи поиска для MCP из метаданных"""
    search_keys = metadata.get('search_keys', {})
    return search_keys.get(mcp_type, [])


# ============= СТРУКТУРА ЗНАНИЙ =============

def load_knowledge_structure() -> tuple:
    """Загружает структуру знаний из YAML файла для марафона"""
    yaml_path = Path(__file__).parent.parent / "knowledge_structure.yaml"

    if not yaml_path.exists():
        logger.warning(f"Файл {yaml_path} не найден, используем пустую структуру")
        return [], {}

    with open(yaml_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    meta = data.get('meta', {})
    sections = {s['id']: s for s in data.get('sections', [])}

    topics = []
    for topic in data.get('topics', []):
        day = topic.get('day', 1)
        topic_type = topic.get('type', 'theory')

        section_id = 'week-1' if day <= 7 else 'week-2'
        section = sections.get(section_id, {})

        topics.append({
            'id': topic.get('id', ''),
            'day': day,
            'type': topic_type,
            'section': section.get('title', f'Неделя {1 if day <= 7 else 2}'),
            'title': topic.get('title', ''),
            'title_en': topic.get('title_en', ''),
            'title_es': topic.get('title_es', ''),
            'title_fr': topic.get('title_fr', ''),
            'main_concept': topic.get('main_concept', ''),
            'related_concepts': topic.get('related_concepts', []),
            'key_insight': topic.get('key_insight', ''),
            'pain_point': topic.get('pain_point', ''),
            'source': topic.get('source', ''),
            'content_prompt': topic.get('content_prompt', ''),
            'task': topic.get('task', ''),
            'work_product': topic.get('work_product', ''),
            'work_product_examples': topic.get('work_product_examples', [])
        })

    def sort_key(t):
        type_order = 0 if t['type'] == 'theory' else 1
        return (t['day'], type_order)

    topics.sort(key=sort_key)

    logger.info(f"Загружено {len(topics)} тем марафона ({meta.get('total_days', 14)} дней)")
    return topics, meta


# Загружаем темы при старте
TOPICS, MARATHON_META = load_knowledge_structure()


def get_topic(index: int) -> Optional[dict]:
    """Получить тему по индексу"""
    return TOPICS[index] if index < len(TOPICS) else None


def get_topic_title(topic: dict, lang: str = 'ru') -> str:
    """Получить название темы на нужном языке."""
    if lang != 'ru':
        localized_key = f'title_{lang}'
        if localized_key in topic:
            return topic[localized_key]
    return topic.get('title', '')


def get_total_topics() -> int:
    """Получить общее количество тем"""
    return len(TOPICS)


def get_marathon_day(intern: dict) -> int:
    """Получить текущий день марафона для участника"""
    start_date = intern.get('marathon_start_date')
    if not start_date:
        topic_index = intern.get('current_topic_index', 0)
        return (topic_index // 2) + 1 if topic_index > 0 else 1

    today = moscow_today()
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    days_passed = (today - start_date).days
    return min(days_passed + 1, MARATHON_DAYS)


def get_topics_for_day(day: int) -> List[dict]:
    """Получить темы для конкретного дня марафона"""
    return [t for t in TOPICS if t['day'] == day]


def get_available_topics(intern: dict) -> List[dict]:
    """Получить доступные темы с учётом правил марафона"""
    marathon_day = get_marathon_day(intern)
    completed = set(intern.get('completed_topics', []))
    topics_today = get_topics_today(intern)

    if topics_today >= MAX_TOPICS_PER_DAY:
        return []

    available = []
    for i, topic in enumerate(TOPICS):
        if i in completed:
            continue
        if topic['day'] > marathon_day:
            continue
        available.append((i, topic))

    return available


def get_sections_progress(completed_topics: list) -> list:
    """Получить прогресс по неделям марафона"""
    weeks = {
        'week-1': {'total': 0, 'completed': 0, 'name': 'Неделя 1: От диагностики к практике'},
        'week-2': {'total': 0, 'completed': 0, 'name': 'Неделя 2: От практики к системе'}
    }

    completed_set = set(completed_topics) if completed_topics else set()

    for i, topic in enumerate(TOPICS):
        week_id = 'week-1' if topic['day'] <= 7 else 'week-2'
        weeks[week_id]['total'] += 1
        if i in completed_set:
            weeks[week_id]['completed'] += 1

    return [weeks['week-1'], weeks['week-2']]


def get_lessons_tasks_progress(completed_topics: list) -> dict:
    """Получить прогресс по Урокам и Заданиям отдельно"""
    result = {
        'lessons': {'total': 0, 'completed': 0},
        'tasks': {'total': 0, 'completed': 0}
    }

    completed_set = set(completed_topics) if completed_topics else set()

    for i, topic in enumerate(TOPICS):
        topic_type = topic.get('type', 'theory')
        if topic_type == 'theory':
            result['lessons']['total'] += 1
            if i in completed_set:
                result['lessons']['completed'] += 1
        else:
            result['tasks']['total'] += 1
            if i in completed_set:
                result['tasks']['completed'] += 1

    return result


def get_days_progress(completed_topics: list, marathon_day: int) -> list:
    """Получить прогресс по дням марафона с разбивкой на уроки и задания"""
    days = []
    completed_set = set(completed_topics)

    for day in range(1, MARATHON_DAYS + 1):
        day_topics = [(i, t) for i, t in enumerate(TOPICS) if t['day'] == day]

        lessons = [(i, t) for i, t in day_topics if t.get('type') == 'theory']
        tasks = [(i, t) for i, t in day_topics if t.get('type') == 'practice']

        lessons_completed = sum(1 for i, _ in lessons if i in completed_set)
        tasks_completed = sum(1 for i, _ in tasks if i in completed_set)
        completed_count = lessons_completed + tasks_completed

        status = 'locked'
        if day <= marathon_day:
            if completed_count == len(day_topics):
                status = 'completed'
            elif completed_count > 0:
                status = 'in_progress'
            else:
                status = 'available'
        elif completed_count > 0:
            # День за пределами calendar day, но есть выполненный контент
            if completed_count == len(day_topics):
                status = 'completed'
            else:
                status = 'in_progress'

        days.append({
            'day': day,
            'total': len(day_topics),
            'completed': completed_count,
            'lessons_total': len(lessons),
            'lessons_completed': lessons_completed,
            'tasks_total': len(tasks),
            'tasks_completed': tasks_completed,
            'status': status
        })

    return days


def score_topic_by_interests(topic: dict, interests: list) -> int:
    """Оценка темы по совпадению с интересами пользователя"""
    if not interests:
        return 0

    score = 0
    interests_lower = [i.lower() for i in interests]

    topic_text = (
        topic.get('title', '').lower() + ' ' +
        topic.get('main_concept', '').lower() + ' ' +
        ' '.join(topic.get('related_concepts', [])).lower() + ' ' +
        topic.get('pain_point', '').lower()
    )

    for interest in interests_lower:
        if interest in topic_text:
            score += 2
        for word in interest.split():
            if len(word) > 3 and word in topic_text:
                score += 1

    return score


def get_next_topic_index(intern: dict) -> Optional[int]:
    """Получить индекс следующей темы с учётом правил марафона"""
    available = get_available_topics(intern)

    if not available:
        return None

    return available[0][0]


def get_practice_for_day(intern: dict, day: int) -> Optional[tuple]:
    """Получить незавершённую практику для указанного дня"""
    completed = set(intern.get('completed_topics', []))

    for i, topic in enumerate(TOPICS):
        if topic['day'] == day and topic.get('type') == 'practice':
            if i not in completed:
                return (i, topic)
    return None


def has_pending_practice(intern: dict) -> Optional[tuple]:
    """Проверить, есть ли незавершённая практика (включая пропущенные дни)"""
    marathon_day = get_marathon_day(intern)
    completed = set(intern.get('completed_topics', []))

    for i, topic in enumerate(TOPICS):
        if topic['day'] > marathon_day:
            break
        if topic.get('type') == 'practice' and i not in completed:
            return (i, topic)
    return None


def get_theory_for_day(intern: dict, day: int) -> Optional[tuple]:
    """Получить незавершённый урок (теорию) для указанного дня"""
    completed = set(intern.get('completed_topics', []))

    for i, topic in enumerate(TOPICS):
        if topic['day'] == day and topic.get('type') == 'theory':
            if i not in completed:
                return (i, topic)
    return None


def has_pending_theory(intern: dict) -> Optional[tuple]:
    """Проверить, есть ли незавершённый урок (включая пропущенные дни)"""
    marathon_day = get_marathon_day(intern)
    completed = set(intern.get('completed_topics', []))

    for i, topic in enumerate(TOPICS):
        if topic['day'] > marathon_day:
            break
        if topic.get('type') == 'theory' and i not in completed:
            return (i, topic)
    return None


def was_theory_sent_today(intern: dict) -> bool:
    """Проверить, была ли теория отправлена сегодня (но ещё не завершена)"""
    marathon_day = get_marathon_day(intern)
    current_idx = intern.get('current_topic_index', 0)

    if current_idx < len(TOPICS):
        current_topic = TOPICS[current_idx]
        if current_topic['day'] == marathon_day and current_topic.get('type') == 'theory':
            if current_idx not in intern.get('completed_topics', []):
                return True
    return False


# ============= ПЕРСОНАЛИЗАЦИЯ =============

EXAMPLE_TEMPLATES = [
    ("аналогия", "Используй аналогию — перенеси структуру или принцип из одной области в другую"),
    ("мини-кейс", "Используй мини-кейс — опиши ситуацию → выбор → последствия"),
    ("контрпример", "Используй контрпример — покажи как НЕ работает, чтобы подчеркнуть как работает правильно"),
    ("сравнение", "Используй сравнение двух подходов — правильный vs неправильный"),
    ("ошибка-мастерство", "Покажи типичную ошибку новичка и приём мастера"),
    ("наблюдение", "Предложи наблюдательный эксперимент — что можно заметить в повседневной жизни"),
]

EXAMPLE_SOURCES = ["работа", "близкая профессиональная сфера", "интерес/хобби", "далёкая сфера для контраста"]


def get_example_rules(intern: dict, marathon_day: int) -> str:
    """Генерирует правила для примеров с ротацией по дню марафона"""
    interests = intern.get('interests', [])
    occupation = intern.get('occupation', '') or 'работа'

    if interests:
        interest_idx = (marathon_day - 1) % len(interests)
        today_interest = interests[interest_idx]
        other_interests = [i for idx, i in enumerate(interests) if idx != interest_idx]
    else:
        today_interest = None
        other_interests = []

    template_idx = (marathon_day - 1) % len(EXAMPLE_TEMPLATES)
    template_name, template_instruction = EXAMPLE_TEMPLATES[template_idx]

    shift = (marathon_day - 1) % len(EXAMPLE_SOURCES)
    rotated_sources = EXAMPLE_SOURCES[shift:] + EXAMPLE_SOURCES[:shift]

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
    """Генерирует промпт для персонализации на основе упрощённого профиля"""
    from config import calc_words
    study_dur = intern.get('study_duration', 15)
    bloom = intern.get('complexity_level', 1) or 1
    words = calc_words(study_dur, bloom)

    interests = ', '.join(intern['interests']) if intern['interests'] else 'не указаны'
    occupation = intern.get('occupation', '') or 'не указано'
    motivation = intern.get('motivation', '') or 'не указано'
    goals = intern.get('goals', '') or 'не указаны'

    example_rules = get_example_rules(intern, marathon_day)

    return f"""
ПРОФИЛЬ СТАЖЕРА:
- Имя: {intern['name']}
- Занятие: {occupation}
- Интересы/хобби: {interests}
- Что важно в жизни: {motivation}
- Что хочет изменить: {goals}
- Время на изучение: {intern['study_duration']} минут (~{words} слов)

ИНСТРУКЦИИ ПО ПЕРСОНАЛИЗАЦИИ:
1. Показывай, как тема помогает достичь того, что стажер хочет изменить: "{goals}"
2. Добавляй мотивационный блок, опираясь на ценности стажера: "{motivation}"
3. Объём текста должен быть рассчитан на {intern['study_duration']} минут чтения (~{words} слов)
4. Пиши простым языком, избегай академического стиля
{example_rules}"""


# ============= СОХРАНЕНИЕ ОТВЕТОВ =============

async def save_answer(chat_id: int, topic_index: int, answer: str):
    """Сохранить ответ стажера"""
    from db import get_pool

    if answer.startswith('[РП]'):
        answer_type = 'work_product'
    elif answer.startswith('[BONUS]'):
        answer_type = 'bonus_answer'
    else:
        answer_type = 'theory_answer'

    async with (await get_pool()).acquire() as conn:
        await conn.execute(
            '''INSERT INTO answers (chat_id, topic_index, answer, answer_type, mode)
               VALUES ($1, $2, $3, $4, $5)''',
            chat_id, topic_index, answer, answer_type, 'marathon'
        )

    try:
        from db.queries.activity import record_active_day
        await record_active_day(chat_id, answer_type, mode='marathon')
    except Exception as e:
        logger.warning(f"Не удалось записать активность для {chat_id}: {e}")
