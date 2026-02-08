"""
AI System Track (@aist_track_bot) — Telegram-бот для системного развития
GitHub: https://github.com/aisystant/aist_track_bot

Миссия: Помочь стажёрам трансформироваться из людей с «непродуктивными убеждениями»
и случайных учеников в систематических учеников, которые собраны и удерживают
внимание на своём системном развитии.

С поддержкой PostgreSQL для хранения данных пользователей.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List

import yaml

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, TelegramObject
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import aiohttp
import asyncpg

from i18n import t, detect_language, get_language_name, SUPPORTED_LANGUAGES
from core.intent import detect_intent, IntentType
from engines.shared import handle_question, ProcessingStage

# Feature flags
from config import USE_STATE_MACHINE

# Импорты из модульных компонентов (вместо дублирующегося кода)
from clients.mcp import mcp_guides, mcp_knowledge, mcp
from clients.claude import ClaudeClient
from db import init_db, get_pool
from db.queries import get_intern, update_intern, get_all_scheduled_interns, get_topics_today
from integrations.telegram.keyboards import (
    kb_experience, kb_difficulty, kb_learning_style, kb_study_duration,
    kb_confirm, kb_learn, kb_update_profile, kb_bloom_level,
    kb_bonus_question, kb_skip_topic, kb_marathon_start,
    kb_submit_work_product, kb_language_select, progress_bar
)

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

# ============= КОНСТАНТЫ =============

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
    "5": {"emoji": "⚡", "name": "5 минут", "words": 500, "desc": "Быстрый обзор"},
    "15": {"emoji": "🕑", "name": "15 минут", "words": 1500, "desc": "Стандартное изучение"},
    "25": {"emoji": "🕓", "name": "25 минут", "words": 2500, "desc": "Полное погружение"}
}

# Уровни сложности вопросов (по таксономии Блума)
# Блум 1: Знание — вопросы "в чём разница"
# Блум 2: Понимание — открытые вопросы
# Блум 3: Применение — анализ, примеры из жизни/работы
BLOOM_LEVELS = {
    1: {
        "emoji": "🔵",
        "name": "Знание",
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

# Автоматическое повышение уровня: после N тем на текущем уровне
BLOOM_AUTO_UPGRADE_AFTER = 7  # после 7 тем уровень повышается

# Лимит тем в день (для развития систематичности)
DAILY_TOPICS_LIMIT = 2
MAX_TOPICS_PER_DAY = 4  # макс тем в день (нагнать 1 день)
MARATHON_DAYS = 14  # длительность марафона

# ============= ОНТОЛОГИЧЕСКИЕ ИНВАРИАНТЫ =============
# Импортируем из config — единый источник истины
from config import ONTOLOGY_RULES

# ============= ЗАГРУЗКА МЕТАДАННЫХ ТЕМ =============

TOPICS_DIR = Path(__file__).parent / "topics"

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

# ============= СОСТОЯНИЯ FSM =============

class OnboardingStates(StatesGroup):
    """Онбординг для марафона"""
    waiting_for_name = State()           # 1. Имя
    waiting_for_occupation = State()     # 2. Чем занимаешься
    waiting_for_interests = State()      # 3. Интересы/хобби
    waiting_for_motivation = State()     # 4. Что важно в жизни
    waiting_for_goals = State()          # 5. Что хочешь изменить
    waiting_for_study_duration = State() # 6. Время на тему
    waiting_for_schedule = State()       # 7. Время напоминания
    waiting_for_start_date = State()     # 8. Дата старта марафона
    confirming_profile = State()

class LearningStates(StatesGroup):
    waiting_for_answer = State()           # ответ на вопрос теории
    waiting_for_work_product = State()     # название рабочего продукта (практика)
    waiting_for_bonus_answer = State()     # ответ на дополнительный вопрос посложнее

class UpdateStates(StatesGroup):
    choosing_field = State()
    updating_name = State()
    updating_occupation = State()
    updating_interests = State()
    updating_motivation = State()
    updating_goals = State()
    updating_duration = State()
    updating_schedule = State()
    updating_bloom_level = State()
    updating_marathon_start = State()


# ============= MIDDLEWARE ДЛЯ ОТЛАДКИ =============

class LoggingMiddleware(BaseMiddleware):
    """Middleware для логирования всех входящих сообщений"""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        from aiogram.fsm.context import FSMContext

        if isinstance(event, Message):
            state: FSMContext = data.get('state')
            current_state = await state.get_state() if state else None
            logger.info(f"[MIDDLEWARE] Получено сообщение: chat_id={event.chat.id}, "
                       f"user_id={event.from_user.id if event.from_user else None}, "
                       f"text={event.text[:50] if event.text else '[no text]'}, "
                       f"state={current_state}")

        return await handler(event, data)


# ============= FSM STORAGE =============
# init_db импортирован из db/ (db.init_db)

class PostgresStorage(BaseStorage):
    """Персистентное хранилище FSM состояний в PostgreSQL"""

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        """Установить состояние"""
        # StateType = Optional[Union[str, State]] - может быть строкой или State объектом
        if state is None:
            state_str = None
        elif isinstance(state, str):
            state_str = state
        else:
            state_str = state.state
        logger.info(f"[FSM] set_state: chat_id={key.chat_id}, user_id={key.user_id}, bot_id={key.bot_id}, state={state_str}")
        async with (await get_pool()).acquire() as conn:
            await conn.execute('''
                INSERT INTO fsm_states (chat_id, state, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET state = $2, updated_at = NOW()
            ''', key.chat_id, state_str)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        """Получить состояние"""
        async with (await get_pool()).acquire() as conn:
            row = await conn.fetchrow(
                'SELECT state FROM fsm_states WHERE chat_id = $1', key.chat_id
            )
            result = row['state'] if row else None
            logger.info(f"[FSM] get_state: chat_id={key.chat_id}, user_id={key.user_id}, bot_id={key.bot_id}, state={result}")
            return result

    async def set_data(self, key: StorageKey, data: dict) -> None:
        """Установить данные состояния"""
        data_str = json.dumps(data, ensure_ascii=False)
        async with (await get_pool()).acquire() as conn:
            await conn.execute('''
                INSERT INTO fsm_states (chat_id, data, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET data = $2, updated_at = NOW()
            ''', key.chat_id, data_str)

    async def get_data(self, key: StorageKey) -> dict:
        """Получить данные состояния"""
        async with (await get_pool()).acquire() as conn:
            row = await conn.fetchrow(
                'SELECT data FROM fsm_states WHERE chat_id = $1', key.chat_id
            )
            if row and row['data']:
                return json.loads(row['data'])
            return {}

    async def close(self) -> None:
        """Закрыть соединение (не требуется, используем общий пул)"""
        pass


async def save_answer(chat_id: int, topic_index: int, answer: str):
    """Сохранить ответ стажера"""
    # Определяем тип ответа
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

    # Записываем активность
    try:
        from db.queries.activity import record_active_day
        await record_active_day(chat_id, answer_type, mode='marathon')
    except Exception as e:
        logger.warning(f"Не удалось записать активность для {chat_id}: {e}")

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


def get_example_rules(intern: dict, marathon_day: int) -> str:
    """Генерирует правила для примеров с ротацией по дню марафона"""
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
    """Генерирует промпт для персонализации на основе упрощённого профиля"""
    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {"words": 1500})

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
- Время на изучение: {intern['study_duration']} минут (~{duration.get('words', 1500)} слов)

ИНСТРУКЦИИ ПО ПЕРСОНАЛИЗАЦИИ:
1. Показывай, как тема помогает достичь того, что стажер хочет изменить: "{goals}"
2. Добавляй мотивационный блок, опираясь на ценности стажера: "{motivation}"
3. Объём текста должен быть рассчитан на {intern['study_duration']} минут чтения (~{duration.get('words', 1500)} слов)
4. Пиши простым языком, избегай академического стиля
{example_rules}"""

# ============= CLAUDE API =============
# ClaudeClient импортируется из clients/claude.py

claude = ClaudeClient()

# ============= СТРУКТУРА ЗНАНИЙ =============

def load_knowledge_structure() -> tuple:
    """Загружает структуру знаний из YAML файла для марафона"""
    yaml_path = Path(__file__).parent / "knowledge_structure.yaml"

    if not yaml_path.exists():
        logger.warning(f"Файл {yaml_path} не найден, используем пустую структуру")
        return [], {}

    with open(yaml_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    meta = data.get('meta', {})
    sections = {s['id']: s for s in data.get('sections', [])}

    # Загружаем темы для марафона
    topics = []
    for topic in data.get('topics', []):
        day = topic.get('day', 1)
        topic_type = topic.get('type', 'theory')

        # Определяем раздел по дню
        section_id = 'week-1' if day <= 7 else 'week-2'
        section = sections.get(section_id, {})

        topics.append({
            'id': topic.get('id', ''),
            'day': day,
            'type': topic_type,  # theory / practice
            'section': section.get('title', f'Неделя {1 if day <= 7 else 2}'),
            'title': topic.get('title', ''),
            'title_en': topic.get('title_en', ''),
            'title_es': topic.get('title_es', ''),
            'main_concept': topic.get('main_concept', ''),
            'related_concepts': topic.get('related_concepts', []),
            'key_insight': topic.get('key_insight', ''),
            'pain_point': topic.get('pain_point', ''),
            'source': topic.get('source', ''),
            # Для генерации контента
            'content_prompt': topic.get('content_prompt', ''),
            # Для практических заданий
            'task': topic.get('task', ''),
            'work_product': topic.get('work_product', ''),
            'work_product_examples': topic.get('work_product_examples', [])
        })

    # Сортируем по дню, затем theory перед practice
    def sort_key(t):
        type_order = 0 if t['type'] == 'theory' else 1
        return (t['day'], type_order)

    topics.sort(key=sort_key)

    logger.info(f"✅ Загружено {len(topics)} тем марафона ({meta.get('total_days', 14)} дней)")
    return topics, meta

# Загружаем темы при старте
TOPICS, MARATHON_META = load_knowledge_structure()

def get_topic(index: int) -> Optional[dict]:
    """Получить тему по индексу"""
    return TOPICS[index] if index < len(TOPICS) else None

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
    """Получить общее количество тем"""
    return len(TOPICS)

def get_marathon_day(intern: dict) -> int:
    """Получить текущий день марафона для участника"""
    start_date = intern.get('marathon_start_date')
    if not start_date:
        # Если дата старта не установлена, вычисляем по прогрессу
        topic_index = intern.get('current_topic_index', 0)
        return (topic_index // 2) + 1 if topic_index > 0 else 1

    today = moscow_today()
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    days_passed = (today - start_date).days
    return min(days_passed + 1, MARATHON_DAYS)  # День 1-14

def get_topics_for_day(day: int) -> List[dict]:
    """Получить темы для конкретного дня марафона"""
    return [t for t in TOPICS if t['day'] == day]

def get_available_topics(intern: dict) -> List[dict]:
    """Получить доступные темы с учётом правил марафона"""
    marathon_day = get_marathon_day(intern)
    completed = set(intern.get('completed_topics', []))
    topics_today = get_topics_today(intern)

    # Нельзя изучать больше MAX_TOPICS_PER_DAY в день
    if topics_today >= MAX_TOPICS_PER_DAY:
        return []

    # Собираем все темы до текущего дня марафона
    available = []
    for i, topic in enumerate(TOPICS):
        if i in completed:
            continue
        if topic['day'] > marathon_day:
            continue  # Нельзя идти вперёд
        available.append((i, topic))

    return available

def get_sections_progress(completed_topics: list) -> list:
    """Получить прогресс по неделям марафона"""
    weeks = {
        'week-1': {'total': 0, 'completed': 0, 'name': 'Неделя 1: От диагностики к практике'},
        'week-2': {'total': 0, 'completed': 0, 'name': 'Неделя 2: От практики к системе'}
    }

    # FIX: Преобразуем в set для O(1) проверки вместо O(n)
    completed_set = set(completed_topics) if completed_topics else set()

    # Собираем темы по неделям
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

    # FIX: Преобразуем в set для O(1) проверки вместо O(n)
    completed_set = set(completed_topics) if completed_topics else set()

    for i, topic in enumerate(TOPICS):
        topic_type = topic.get('type', 'theory')
        if topic_type == 'theory':
            result['lessons']['total'] += 1
            if i in completed_set:
                result['lessons']['completed'] += 1
        else:  # practice
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

        # Разделяем на уроки (theory) и задания (practice)
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

    # Проверяем title, main_concept, related_concepts
    topic_text = (
        topic.get('title', '').lower() + ' ' +
        topic.get('main_concept', '').lower() + ' ' +
        ' '.join(topic.get('related_concepts', [])).lower() + ' ' +
        topic.get('pain_point', '').lower()
    )

    for interest in interests_lower:
        # Простой поиск подстроки
        if interest in topic_text:
            score += 2
        # Поиск по словам
        for word in interest.split():
            if len(word) > 3 and word in topic_text:
                score += 1

    return score

def get_next_topic_index(intern: dict) -> Optional[int]:
    """Получить индекс следующей темы с учётом правил марафона"""
    available = get_available_topics(intern)

    if not available:
        return None

    # Возвращаем первую доступную тему (они уже отсортированы по дню и типу)
    return available[0][0]


def get_practice_for_day(intern: dict, day: int) -> Optional[tuple]:
    """Получить незавершённую практику для указанного дня

    Returns:
        (index, topic) если есть незавершённая практика, иначе None
    """
    completed = set(intern.get('completed_topics', []))

    for i, topic in enumerate(TOPICS):
        if topic['day'] == day and topic.get('type') == 'practice':
            if i not in completed:
                return (i, topic)
    return None


def has_pending_practice(intern: dict) -> Optional[tuple]:
    """Проверить, есть ли незавершённая практика (включая пропущенные дни)

    Returns:
        (index, topic) если есть, иначе None
    """
    marathon_day = get_marathon_day(intern)
    completed = set(intern.get('completed_topics', []))

    for i, topic in enumerate(TOPICS):
        if topic['day'] > marathon_day:
            break
        if topic.get('type') == 'practice' and i not in completed:
            return (i, topic)
    return None


def get_theory_for_day(intern: dict, day: int) -> Optional[tuple]:
    """Получить незавершённый урок (теорию) для указанного дня

    Returns:
        (index, topic) если есть незавершённый урок, иначе None
    """
    completed = set(intern.get('completed_topics', []))

    for i, topic in enumerate(TOPICS):
        if topic['day'] == day and topic.get('type') == 'theory':
            if i not in completed:
                return (i, topic)
    return None


def has_pending_theory(intern: dict) -> Optional[tuple]:
    """Проверить, есть ли незавершённый урок (включая пропущенные дни)

    Returns:
        (index, topic) если есть, иначе None
    """
    marathon_day = get_marathon_day(intern)
    completed = set(intern.get('completed_topics', []))

    for i, topic in enumerate(TOPICS):
        if topic['day'] > marathon_day:
            break
        if topic.get('type') == 'theory' and i not in completed:
            return (i, topic)
    return None


def was_theory_sent_today(intern: dict) -> bool:
    """Проверить, была ли теория отправлена сегодня (но ещё не завершена)

    Логика: если current_topic_index указывает на теорию текущего дня,
    значит теория была отправлена, но ответ ещё не получен.
    """
    marathon_day = get_marathon_day(intern)
    current_idx = intern.get('current_topic_index', 0)

    # Проверяем, указывает ли current_topic_index на теорию текущего дня
    if current_idx < len(TOPICS):
        current_topic = TOPICS[current_idx]
        if current_topic['day'] == marathon_day and current_topic.get('type') == 'theory':
            # Теория текущего дня — проверяем, не пройдена ли она
            if current_idx not in intern.get('completed_topics', []):
                return True
    return False

# ============= РОУТЕР =============

router = Router()

# --- Онбординг ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)

    if intern['onboarding_completed']:
        lang = intern.get('language', 'ru')

        # Определяем текущий режим
        from config import Mode
        current_mode = intern.get('mode', Mode.MARATHON)
        mode_emoji = "🏃" if current_mode == Mode.MARATHON else "📚"
        mode_name = t('help.marathon', lang) if current_mode == Mode.MARATHON else t('help.feed', lang)

        # Прогресс активности
        from db.queries.activity import get_activity_stats
        stats = await get_activity_stats(message.chat.id)
        total_active = stats.get('total', 0)
        marathon_day = get_marathon_day(intern)

        await message.answer(
            t('welcome.returning', lang, name=intern['name']) + "\n" +
            f"{mode_emoji} {t('welcome.current_mode', lang)}: *{mode_name}*\n" +
            f"📊 {t('welcome.activity_progress', lang)}: {total_active} {t('shared.of', lang)} {marathon_day}\n\n" +
            t('commands.learn', lang) + "\n" +
            t('commands.progress', lang) + "\n" +
            t('commands.profile', lang) + "\n" +
            t('commands.update', lang) + "\n" +
            t('commands.mode', lang),
            parse_mode="Markdown"
        )
        return

    # Определяем язык интерфейса пользователя
    lang = detect_language(message.from_user.language_code)

    if lang in SUPPORTED_LANGUAGES:
        welcome_text = (
            t('welcome.greeting', lang) + "\n" +
            t('welcome.intro', lang) + "\n\n" +
            t('welcome.ask_name', lang)
        )
    else:
        # Для неизвестных языков — двуязычное (EN + RU)
        welcome_text = (
            t('welcome.greeting', 'en') + "\n" +
            t('welcome.intro', 'en') + "\n" +
            t('welcome.ask_name', 'en') + "\n\n" +
            "━━━━━━━━━━━━━━━━━━\n\n" +
            t('welcome.greeting', 'ru') + "\n" +
            t('welcome.intro', 'ru') + "\n" +
            t('welcome.ask_name', 'ru')
        )
        lang = 'ru'  # По умолчанию русский

    # Сохраняем определённый язык для дальнейшего использования
    await state.update_data(lang=lang)

    await message.answer(welcome_text)
    await state.set_state(OnboardingStates.waiting_for_name)


async def get_lang(state: FSMContext, intern: dict = None) -> str:
    """Получить язык из state или из профиля пользователя"""
    data = await state.get_data()
    if 'lang' in data:
        return data['lang']
    if intern and 'language' in intern:
        return intern['language']
    return 'ru'


@router.message(OnboardingStates.waiting_for_name)
async def on_name(message: Message, state: FSMContext):
    lang = await get_lang(state)
    name = message.text.strip()
    await update_intern(message.chat.id, name=name, language=lang)
    await message.answer(
        t('onboarding.nice_to_meet', lang, name=name) + "\n\n" +
        t('onboarding.ask_occupation', lang) + "\n\n" +
        t('onboarding.ask_occupation_hint', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_occupation)

@router.message(OnboardingStates.waiting_for_occupation)
async def on_occupation(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await update_intern(message.chat.id, occupation=message.text.strip())
    await message.answer(
        t('onboarding.ask_interests', lang) + "\n\n" +
        t('onboarding.ask_interests_hint', lang) + "\n\n" +
        t('onboarding.ask_interests_why', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_interests)

@router.message(OnboardingStates.waiting_for_interests)
async def on_interests(message: Message, state: FSMContext):
    lang = await get_lang(state)
    interests = [i.strip() for i in message.text.replace(',', ';').split(';') if i.strip()]
    await update_intern(message.chat.id, interests=interests)
    await message.answer(
        f"*{t('onboarding.ask_values', lang)}*\n\n" +
        t('onboarding.ask_values_hint', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_motivation)

@router.message(OnboardingStates.waiting_for_motivation)
async def on_motivation(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await update_intern(message.chat.id, motivation=message.text.strip())
    await message.answer(
        f"*{t('onboarding.ask_goals', lang)}*\n\n" +
        t('onboarding.ask_goals_hint', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_goals)

@router.message(OnboardingStates.waiting_for_goals)
async def on_goals(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await update_intern(message.chat.id, goals=message.text.strip())
    await message.answer(
        t('onboarding.ask_duration', lang) + "\n\n",
        parse_mode="Markdown",
        reply_markup=kb_study_duration(lang)
    )
    await state.set_state(OnboardingStates.waiting_for_study_duration)

@router.callback_query(OnboardingStates.waiting_for_study_duration, F.data.startswith("duration_"))
async def on_duration(callback: CallbackQuery, state: FSMContext):
    lang = await get_lang(state)
    duration = int(callback.data.replace("duration_", ""))
    await update_intern(callback.message.chat.id, study_duration=duration)
    await callback.answer()
    await callback.message.edit_text(
        t('onboarding.ask_time', lang) + "\n\n" +
        t('onboarding.ask_time_hint', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_schedule)

@router.message(OnboardingStates.waiting_for_schedule)
async def on_schedule(message: Message, state: FSMContext):
    lang = await get_lang(state)
    try:
        h, m = map(int, message.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except:
        await message.answer(t('errors.try_again', lang) + " (ЧЧ:ММ)")
        return

    # Нормализуем формат времени (с ведущими нулями)
    normalized_time = f"{h:02d}:{m:02d}"
    await update_intern(message.chat.id, schedule_time=normalized_time)

    await message.answer(
        f"🗓 *{t('onboarding.ask_start_date', lang)}*\n\n" +
        t('modes.marathon_desc', lang),
        parse_mode="Markdown",
        reply_markup=kb_marathon_start(lang)
    )
    await state.set_state(OnboardingStates.waiting_for_start_date)

@router.callback_query(OnboardingStates.waiting_for_start_date, F.data.startswith("start_"))
async def on_start_date(callback: CallbackQuery, state: FSMContext):
    today = moscow_today()

    if callback.data == "start_today":
        start_date = today
    elif callback.data == "start_tomorrow":
        start_date = today + timedelta(days=1)
    else:  # start_day_after
        start_date = today + timedelta(days=2)

    await update_intern(callback.message.chat.id, marathon_start_date=start_date)
    await callback.answer()

    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'

    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    interests_str = ', '.join(intern['interests']) if intern['interests'] else t('profile.not_specified_plural', lang)
    motivation_short = intern['motivation'][:100] + '...' if len(intern['motivation']) > 100 else intern['motivation']
    goals_short = intern['goals'][:100] + '...' if len(intern['goals']) > 100 else intern['goals']

    await callback.message.edit_text(
        f"📋 *{t('profile.your_profile', lang)}:*\n\n"
        f"👤 *{t('profile.name_label', lang)}:* {intern['name']}\n"
        f"💼 *{t('profile.occupation_label', lang)}:* {intern['occupation']}\n"
        f"🎨 *{t('profile.interests_label', lang)}:* {interests_str}\n\n"
        f"💫 *{t('profile.what_important', lang)}:* {motivation_short}\n"
        f"🎯 *{t('profile.what_change', lang)}:* {goals_short}\n\n"
        f"{duration.get('emoji', '')} {duration.get('name', '')} {t('profile.per_topic', lang)}\n"
        f"⏰ {t('profile.reminder_at', lang)} {intern['schedule_time']}\n"
        f"🗓 {t('profile.marathon_start', lang)}: *{start_date.strftime('%d.%m.%Y')}*\n\n"
        f"{t('profile.all_correct', lang)}",
        parse_mode="Markdown",
        reply_markup=kb_confirm(lang)
    )
    await state.set_state(OnboardingStates.confirming_profile)

@router.callback_query(OnboardingStates.confirming_profile, F.data == "confirm")
async def on_confirm(callback: CallbackQuery, state: FSMContext):
    await update_intern(callback.message.chat.id, onboarding_completed=True)
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'
    marathon_day = get_marathon_day(intern)
    start_date = intern.get('marathon_start_date')

    await callback.answer(t('update.saved', lang))

    # Определяем, когда старт
    if start_date:
        today = moscow_today()
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        if start_date > today:
            start_msg = f"🗓 *{t('profile.marathon_will_start', lang, date=start_date.strftime('%d.%m.%Y'))}*"
            can_start_now = False
        else:
            start_msg = f"🗓 *{t('progress.day', lang, day=marathon_day, total=MARATHON_DAYS)}*"
            can_start_now = True
    else:
        start_msg = f"🗓 {t('profile.date_not_set', lang)}"
        can_start_now = False

    # Приветственное сообщение для марафона
    await callback.message.edit_text(
        f"🎉 *{t('welcome.marathon_welcome', lang, name=intern['name'])}*\n\n"
        f"{t('welcome.marathon_intro', lang)}\n"
        f"📅 {t('welcome.marathon_days_info', lang, days=MARATHON_DAYS)}\n"
        f"⏱ {t('welcome.marathon_duration_info', lang, minutes=intern['study_duration'])}\n"
        f"⏰ {t('welcome.marathon_reminders_info', lang, time=intern['schedule_time'])}\n\n"
        f"{start_msg}",
        parse_mode="Markdown",
        reply_markup=kb_learn(lang)
    )
    await state.clear()

@router.callback_query(OnboardingStates.confirming_profile, F.data == "restart")
async def on_restart(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("Давайте заново!\n\nКак вас зовут?")
    await state.set_state(OnboardingStates.waiting_for_name)

# --- Обучение ---

@router.message(Command("learn"))
async def cmd_learn(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    if not intern['onboarding_completed']:
        lang = intern.get('language', 'ru') or 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    # State Machine routing
    if state_machine is not None:
        logger.info(f"[SM] /learn command routed to StateMachine for chat_id={message.chat.id}")
        try:
            await state.clear()  # Очищаем legacy FSM state перед SM routing
            await state_machine.go_to(intern, "workshop.marathon.lesson")
            return
        except Exception as e:
            logger.error(f"[SM] Error routing /learn to StateMachine: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Fallback to legacy

    await send_topic(message.chat.id, state, message.bot)

@router.callback_query(F.data == "learn")
async def cb_learn(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup()

    # State Machine routing
    if state_machine is not None:
        logger.info(f"[SM] learn callback routed to StateMachine for chat_id={callback.message.chat.id}")
        try:
            await state.clear()  # Очищаем legacy FSM state перед SM routing
            intern = await get_intern(callback.message.chat.id)
            if intern:
                await state_machine.go_to(intern, "workshop.marathon.lesson")
                return
        except Exception as e:
            logger.error(f"[SM] Error routing learn callback to StateMachine: {e}")
            # Fallback to legacy

    await send_topic(callback.message.chat.id, state, callback.bot)

@router.callback_query(F.data == "later")
async def cb_later(callback: CallbackQuery):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'
    await callback.answer()
    await callback.message.edit_text(t('fsm.see_you_later', lang, time=intern['schedule_time']))

# --- Лента (State Machine routing) ---

@router.message(Command("feed"))
async def cmd_feed(message: Message, state: FSMContext):
    """Команда /feed - вход в режим Лента через State Machine"""
    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    # State Machine routing
    if state_machine is not None:
        logger.info(f"[SM] /feed command routed to StateMachine for chat_id={message.chat.id}")
        try:
            await state.clear()  # Очищаем legacy FSM state перед SM routing
            await state_machine.go_to(intern, "feed.topics")
            return
        except Exception as e:
            logger.error(f"[SM] Error routing /feed to StateMachine: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Fallback: показываем сообщение об ошибке
            lang = intern.get('language', 'ru') or 'ru'
            await message.answer(t('errors.try_again', lang))
    else:
        # Если State Machine не включен, показываем сообщение
        lang = intern.get('language', 'ru') or 'ru'
        await message.answer(t('feed.not_available', lang))


@router.callback_query(F.data == "feed")
async def cb_feed(callback: CallbackQuery, state: FSMContext):
    """Callback для входа в Ленту через State Machine"""
    await callback.answer()
    await callback.message.edit_reply_markup()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return

    # State Machine routing
    if state_machine is not None:
        logger.info(f"[SM] feed callback routed to StateMachine for chat_id={callback.message.chat.id}")
        try:
            await state.clear()  # Очищаем legacy FSM state перед SM routing
            await state_machine.go_to(intern, "feed.topics")
            return
        except Exception as e:
            logger.error(f"[SM] Error routing feed callback to StateMachine: {e}")
            lang = intern.get('language', 'ru') or 'ru'
            await callback.message.answer(t('errors.try_again', lang))


@router.callback_query(F.data.startswith("feed_"))
async def cb_feed_actions(callback: CallbackQuery, state: FSMContext):
    """Обработка всех Feed-специфичных callback-ов через State Machine."""
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        await callback.answer()
        return

    if state_machine is not None:
        data = callback.data
        logger.info(f"[SM] Feed callback '{data}' for chat_id={callback.message.chat.id}")

        try:
            # Определяем целевой стейт по типу callback-а
            # Если пользователь не в Feed-стейте, сначала переходим туда
            current_state = intern.get('current_state', '')

            if data == "feed_get_digest":
                # Получить дайджест → переходим в feed.digest
                await callback.answer()
                await callback.message.edit_reply_markup()
                await state.clear()  # Очищаем legacy FSM state перед SM routing
                await state_machine.go_to(intern, "feed.digest")
                return

            elif data == "feed_topics_menu":
                # Меню тем → переходим в feed.digest и показываем меню тем
                await callback.answer()
                await callback.message.edit_reply_markup()
                await state.clear()  # Очищаем legacy FSM state перед SM routing
                # Передаём контекст для показа меню тем
                await state_machine.go_to(intern, "feed.digest", context={"show_topics_menu": True})
                return

            elif current_state.startswith("feed."):
                # Пользователь уже в Feed-стейте — передаём callback туда
                await state_machine.handle_callback(intern, callback)
                return

            else:
                # Для других feed_ callback-ов переходим в feed.digest по умолчанию
                logger.warning(f"[SM] User in state '{current_state}' clicked '{data}', routing to feed.digest")
                await callback.answer()
                await callback.message.edit_reply_markup()
                await state.clear()  # Очищаем legacy FSM state перед SM routing
                await state_machine.go_to(intern, "feed.digest")
                return

        except Exception as e:
            logger.error(f"[SM] Error handling feed callback: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await callback.answer()
            lang = intern.get('language', 'ru') or 'ru'
            await callback.message.answer(t('errors.try_again', lang))
    else:
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('feed.not_available', lang))


async def is_in_sm_settings_state(callback: CallbackQuery) -> bool:
    """Фильтр: проверяет что пользователь в common.settings стейте State Machine."""
    if state_machine is None:
        return False
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return False
    return intern.get('current_state') == "common.settings"


@router.callback_query(
    F.data.startswith("upd_") | F.data.startswith("settings_") | F.data.startswith("duration_") | F.data.startswith("bloom_") | F.data.startswith("lang_"),
    is_in_sm_settings_state
)
async def cb_settings_actions(callback: CallbackQuery, state: FSMContext):
    """Обработка Settings-специфичных callback-ов через State Machine."""
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        await callback.answer()
        return

    data = callback.data
    logger.info(f"[SM] Settings callback '{data}' for chat_id={callback.message.chat.id}")
    try:
        await state_machine.handle_callback(intern, callback)
    except Exception as e:
        logger.error(f"[SM] Error handling settings callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))


@router.message(Command("progress"))
async def cmd_progress(message: Message):
    """Короткий отчёт прогресса за текущую неделю"""
    from db.queries.answers import get_weekly_marathon_stats, get_weekly_feed_stats
    from db.queries.activity import get_activity_stats

    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('progress.first_start', lang))
        return

    chat_id = message.chat.id
    lang = intern.get('language', 'ru') or 'ru'

    try:
        # Получаем статистику
        activity_stats = await get_activity_stats(chat_id)
        marathon_stats = await get_weekly_marathon_stats(chat_id)
        feed_stats = await get_weekly_feed_stats(chat_id)
    except Exception as e:
        logger.error(f"Ошибка получения статистики для {chat_id}: {e}")
        activity_stats = {'days_active_this_week': 0}
        marathon_stats = {'work_products': 0}
        feed_stats = {'digests': 0, 'fixations': 0}

    # Общие данные
    days_active_week = activity_stats.get('days_active_this_week', 0)

    # Марафон
    marathon_day = get_marathon_day(intern)
    lessons_week = marathon_stats.get('theory_answers', 0)
    tasks_week = marathon_stats.get('work_products', 0)

    # Лента - получаем темы
    try:
        from engines.feed.engine import FeedEngine
        feed_engine = FeedEngine(chat_id)
        feed_status = await feed_engine.get_status()
        feed_topics = feed_status.get('topics', [])
        feed_topics_text = ", ".join(feed_topics) if feed_topics else t('progress.topics_not_selected', lang)
    except Exception as e:
        logger.error(f"Ошибка получения статуса ленты для {chat_id}: {e}")
        feed_topics_text = t('progress.topics_not_selected', lang)

    text = f"{t('progress.title', lang, name=intern['name'])}\n\n"
    text += f"📈 {t('progress.active_days_week', lang)}: {days_active_week}\n\n"

    # Марафон
    text += f"🏃 *{t('progress.marathon_title', lang)}*\n"
    text += f"{t('progress.day_of_total', lang, day=marathon_day, total=MARATHON_DAYS)}\n"
    text += f"📖 {t('progress.lessons', lang)}: {lessons_week}. 📝 {t('progress.tasks', lang)}: {tasks_week}\n\n"

    # Лента
    text += f"📚 *{t('progress.feed_title', lang)}*\n"
    text += f"{t('progress.digests', lang)}: {feed_stats.get('digests', 0)}. {t('progress.fixations', lang)}: {feed_stats.get('fixations', 0)}\n"
    text += f"{t('progress.topics', lang)}: {feed_topics_text}"

    # Кнопки
    from config import Mode
    current_mode = intern.get('mode', Mode.MARATHON)

    # Кнопка продолжения зависит от режима
    if current_mode == Mode.FEED:
        continue_btn = InlineKeyboardButton(text=f"📖 {t('buttons.get_digest', lang)}", callback_data="feed_get_digest")
    else:
        continue_btn = InlineKeyboardButton(text=f"📚 {t('buttons.continue_learning', lang)}", callback_data="learn")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [continue_btn],
        [
            InlineKeyboardButton(text=f"📊 {t('progress.full_report', lang)}", callback_data="progress_full"),
            InlineKeyboardButton(text=f"⚙️ {t('buttons.settings', lang)}", callback_data="go_update")
        ]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data == "progress_full")
async def show_full_progress(callback: CallbackQuery):
    """Полный отчёт с начала использования бота"""
    await callback.answer()  # Сразу отвечаем, чтобы убрать "крутилку" с кнопки

    try:
        from db.queries.answers import get_total_stats, get_work_products_by_day

        chat_id = callback.message.chat.id
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

        if not intern:
            await callback.message.edit_text(t('profile.not_found', lang))
            return

        # Получаем полную статистику
        try:
            total_stats = await get_total_stats(chat_id)
        except Exception as e:
            logger.error(f"Ошибка получения total_stats: {e}")
            total_stats = {}

        # Дата регистрации
        reg_date = total_stats.get('registered_at')
        if reg_date:
            date_str = reg_date.strftime('%d.%m.%Y')
        else:
            date_str = "—"

        days_since = total_stats.get('days_since_start', 1)
        total_active = total_stats.get('total_active_days', 0)

        # Марафон
        marathon_day = get_marathon_day(intern)

        # Прогресс по Урокам и Заданиям
        progress = get_lessons_tasks_progress(intern.get('completed_topics', []))

        # Прогресс по дням
        try:
            wp_by_day = await get_work_products_by_day(chat_id, TOPICS)
        except Exception as e:
            logger.error(f"Ошибка получения wp_by_day: {e}")
            wp_by_day = {}

        days_progress = get_days_progress(intern.get('completed_topics', []), marathon_day)

        # Формируем текст по дням (обратный порядок: сначала последний день)
        days_text = ""
        visible_days = [d for d in days_progress if d['day'] <= marathon_day and d['status'] != 'locked']
        for d in reversed(visible_days):
            day_num = d['day']
            wp_count = wp_by_day.get(day_num, 0)

            if d['status'] == 'completed':
                emoji = "✅"
            elif d['status'] == 'in_progress':
                emoji = "🔄"
            elif d['status'] == 'available':
                emoji = "📍"
            else:
                continue

            # Формат: День N: Урок: X | Задание: Y | РП: Z
            lesson_text = f"{t('progress.lesson_short', lang)}: {d['lessons_completed']}"
            task_text = f"{t('progress.task_short', lang)}: {d['tasks_completed']}"
            wp_text = f"{t('progress.wp_short', lang)}: {wp_count}"
            days_text += f"   {emoji} {t('progress.day_text', lang, day=day_num)}: {lesson_text} | {task_text} | {wp_text}\n"

        # Лента
        try:
            from engines.feed.engine import FeedEngine
            feed_engine = FeedEngine(chat_id)
            feed_status = await feed_engine.get_status()
            feed_topics = feed_status.get('topics', [])
            feed_topics_text = ", ".join(feed_topics) if feed_topics else t('progress.topics_not_selected', lang)
        except Exception as e:
            logger.error(f"Ошибка получения feed_status: {e}")
            feed_topics_text = "—"

        name = intern.get('name', 'User')
        text = f"📊 *{t('progress.full_report_title', lang, date=date_str, name=name)}*\n\n"
        text += f"📈 *{t('progress.active_days_both', lang)}:* {total_active} {t('shared.of', lang)} {days_since}\n\n"

        # Марафон
        text += f"🏃 *{t('progress.marathon_title', lang)}*\n"
        text += f"{t('progress.day', lang, day=marathon_day, total=MARATHON_DAYS)}\n"
        text += f"📖 {t('progress.lessons', lang)}: {progress['lessons']['completed']}/{progress['lessons']['total']}\n"
        text += f"📝 {t('progress.tasks', lang)}: {progress['tasks']['completed']}/{progress['tasks']['total']}\n"
        text += f"{t('progress.work_products_count', lang)}: {total_stats.get('total_work_products', 0)}\n"

        # По дням
        if days_text:
            text += f"\n📋 *{t('progress.by_days', lang)}:*\n{days_text}"

        # Отставание (сколько дней контента пропущено)
        # Считаем количество полностью завершённых дней марафона
        days_progress = get_days_progress(intern.get('completed_topics', []), marathon_day)
        completed_days = sum(1 for d in days_progress if d['status'] == 'completed')
        lag = marathon_day - completed_days
        text += f"{t('progress.lag', lang)}: {lag} {t('progress.days', lang)}\n"

        # Лента
        text += f"\n📚 *{t('progress.feed_title', lang)}*\n"
        text += f"{t('progress.digests_count', lang)}: {total_stats.get('total_digests', 0)}\n"
        text += f"{t('progress.fixations_count', lang)}: {total_stats.get('total_fixations', 0)}\n"
        text += f"{t('progress.topics_colon', lang)}: {feed_topics_text}"

        # Кнопки
        from config import Mode
        current_mode = intern.get('mode', Mode.MARATHON)

        if current_mode == Mode.FEED:
            continue_btn = InlineKeyboardButton(text=f"📖 {t('progress.get_digest', lang)}", callback_data="feed_get_digest")
        else:
            continue_btn = InlineKeyboardButton(text=f"📚 {t('progress.continue_learning', lang)}", callback_data="learn")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [continue_btn],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="progress_back")]
        ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка в show_full_progress: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.message.edit_text(
            f"{t('progress.full_report_error', lang)}\n\n/progress"
        )


@router.callback_query(F.data == "progress_back")
async def progress_back(callback: CallbackQuery):
    """Возврат к короткому отчёту"""
    await callback.answer()

    try:
        # Удаляем текущее сообщение и отправляем подсказку
        await callback.message.delete()
        await callback.message.answer(
            "Для обновлённого отчёта используйте /progress"
        )
    except Exception as e:
        logger.error(f"Ошибка в progress_back: {e}")
        await callback.message.edit_text(
            "/progress — посмотреть прогресс"
        )


@router.callback_query(F.data == "go_update")
async def go_to_update(callback: CallbackQuery, state: FSMContext):
    """Переход к настройкам"""
    await callback.answer()
    intern = await get_intern(callback.message.chat.id)

    # State Machine routing
    if state_machine is not None and intern:
        logger.info(f"[SM] go_update callback routed to StateMachine for chat_id={callback.message.chat.id}")
        try:
            await state.clear()  # Очищаем legacy FSM state перед SM routing
            await callback.message.delete()
            await state_machine.go_to(intern, "common.settings")
            return
        except Exception as e:
            logger.error(f"[SM] Error routing go_update to StateMachine: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Fallback to legacy

    # Legacy: показываем экран настроек напрямую
    if not intern:
        return

    lang = intern.get('language', 'ru') or 'ru'
    study_duration = intern.get('study_duration') or 15
    bloom_level = intern.get('bloom_level') or 1
    bloom_emojis = {1: '🔵', 2: '🟡', 3: '🔴'}

    start_date = intern.get('marathon_start_date')
    if start_date:
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        marathon_start_str = start_date.strftime('%d.%m.%Y')
    else:
        marathon_start_str = "—"

    marathon_day = get_marathon_day(intern)
    interests_str = ', '.join(intern.get('interests', [])) if intern.get('interests') else '—'
    motivation_short = intern.get('motivation', '')[:80] + '...' if len(intern.get('motivation', '')) > 80 else intern.get('motivation', '') or '—'
    goals_short = (intern.get('goals') or '')[:80] + '...' if len(intern.get('goals') or '') > 80 else intern.get('goals') or '—'

    await callback.message.delete()
    await callback.message.answer(
        f"👤 *{intern.get('name', '—')}*\n"
        f"💼 {intern.get('occupation', '') or '—'}\n"
        f"🎨 {interests_str}\n\n"
        f"💫 {motivation_short}\n"
        f"🎯 {goals_short}\n\n"
        f"{t(f'duration.minutes_{study_duration}', lang)}\n"
        f"{bloom_emojis.get(bloom_level, '🔵')} {t(f'bloom.level_{bloom_level}_short', lang)}\n"
        f"🗓 {marathon_start_str} ({t('progress.day', lang, day=marathon_day, total=14)})\n"
        f"⏰ {intern.get('schedule_time', '09:00')}\n"
        f"🌐 {get_language_name(lang)}\n\n"
        f"*{t('settings.what_to_change', lang)}*",
        parse_mode="Markdown",
        reply_markup=kb_update_profile(lang)
    )
    await state.set_state(UpdateStates.choosing_field)


@router.callback_query(F.data == "go_progress")
async def go_to_progress(callback: CallbackQuery):
    """Переход к прогрессу"""
    await callback.answer()
    await cmd_progress(callback.message)


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')

    if not intern['onboarding_completed']:
        await message.answer(t('profile.first_start', lang))
        return

    study_duration = intern['study_duration']
    bloom_level = intern['bloom_level']
    bloom_emojis = {1: '🔵', 2: '🟡', 3: '🔴'}

    interests_str = ', '.join(intern['interests']) if intern['interests'] else t('profile.not_specified', lang)
    motivation_short = intern['motivation'][:100] + '...' if len(intern.get('motivation', '')) > 100 else intern.get('motivation', '')
    goals_short = intern['goals'][:100] + '...' if len(intern['goals']) > 100 else intern['goals']

    await message.answer(
        f"👤 *{intern['name']}*\n"
        f"💼 {intern.get('occupation', '')}\n"
        f"🎨 {interests_str}\n\n"
        f"💫 *{t('profile.what_important', lang)}:* {motivation_short or t('profile.not_specified', lang)}\n"
        f"🎯 *{t('profile.what_change', lang)}:* {goals_short}\n\n"
        f"{t(f'duration.minutes_{study_duration}', lang)}\n"
        f"{bloom_emojis.get(bloom_level, '🔵')} {t(f'bloom.level_{bloom_level}_short', lang)}\n"
        f"⏰ {t('profile.reminder_at', lang)} {intern['schedule_time']}\n"
        f"🌐 {get_language_name(lang)}\n\n"
        f"🆔 `{message.chat.id}`\n\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    await message.answer(
        f"📖 *{t('help.title', lang)}*\n\n"
        f"*{t('help.modes_title', lang)}:*\n"
        f"🏃 *{t('help.marathon', lang)}* — {t('help.marathon_desc', lang)}\n\n"
        f"📚 *{t('help.feed', lang)}* — {t('help.feed_desc', lang)}\n\n"
        f"💬 {t('help.ai_questions', lang)}\n"
        f"_{t('help.ai_questions_example', lang)}_\n\n"
        f"📋 *{t('help.commands_title', lang)}:*\n"
        f"{t('commands.learn', lang)}\n"
        f"/feed — {t('help.feed_cmd', lang)}\n"
        f"/mode — {t('menu.mode', lang)}\n"
        f"{t('commands.progress', lang)}\n"
        f"{t('commands.profile', lang)}\n"
        f"{t('commands.update', lang)}\n\n"
        f"🔄 *{t('help.how_it_works', lang)}:*\n"
        f"{t('help.step1', lang)}\n"
        f"{t('help.step2', lang)}\n"
        f"{t('help.step3', lang)}\n"
        f"{t('help.step4', lang)}\n"
        f"{t('help.step5', lang)}\n\n"
        f"💡 _{t('help.schedule_note', lang)}_\n\n"
        f"💬 {t('help.feedback', lang)}: @tserentserenov\n\n"
        "🔗 [Мастерская инженеров-менеджеров](https://system-school.ru/)",
        parse_mode="Markdown"
    )

# --- Linear OAuth (тестовая интеграция) ---

@router.message(Command("linear"))
async def cmd_linear(message: Message):
    """Команда для интеграции с Linear.

    Подкоманды:
    - /linear — показать статус и получить ссылку авторизации
    - /linear tasks — показать свои задачи
    - /linear disconnect — отключить интеграцию
    """
    from clients.linear_oauth import linear_oauth
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    telegram_user_id = message.chat.id

    # Парсим подкоманду
    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    subcommand = parts[1].lower() if len(parts) > 1 else None

    # Проверяем подключение
    is_connected = linear_oauth.is_connected(telegram_user_id)

    if subcommand == "disconnect":
        if is_connected:
            linear_oauth.disconnect(telegram_user_id)
            await message.answer("✅ Linear отключён.")
        else:
            await message.answer("ℹ️ Linear не был подключён.")
        return

    if subcommand == "tasks":
        if not is_connected:
            await message.answer(
                "❌ Linear не подключён.\n\n"
                "Используйте /linear для подключения."
            )
            return

        # Получаем задачи
        await message.answer("⏳ Загружаю задачи...")
        issues = await linear_oauth.get_my_issues(telegram_user_id, limit=10)

        if issues is None:
            await message.answer("❌ Не удалось получить задачи. Попробуйте переподключиться: /linear disconnect, затем /linear")
            return

        if not issues:
            await message.answer("📭 У вас нет назначенных задач.")
            return

        # Форматируем список задач
        lines = ["📋 *Ваши задачи в Linear:*\n"]
        for issue in issues:
            state_name = issue.get("state", {}).get("name", "?")
            identifier = issue.get("identifier", "?")
            title = issue.get("title", "Без названия")
            url = issue.get("url", "")

            lines.append(f"• [{identifier}]({url}) — {title}\n  _{state_name}_")

        await message.answer("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)
        return

    # Основная команда /linear — показать статус или ссылку авторизации
    if is_connected:
        viewer = await linear_oauth.get_viewer(telegram_user_id)
        name = viewer.get("name", "пользователь") if viewer else "пользователь"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои задачи", callback_data="linear_tasks")],
            [InlineKeyboardButton(text="🔌 Отключить Linear", callback_data="linear_disconnect")]
        ])

        await message.answer(
            f"✅ *Linear подключён*\n\n"
            f"Вы авторизованы как: *{name}*",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        # Генерируем ссылку авторизации
        try:
            auth_url, state = linear_oauth.get_authorization_url(telegram_user_id)

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Подключить Linear", url=auth_url)]
            ])

            await message.answer(
                "🔗 *Подключение к Linear*\n\n"
                "Нажмите кнопку ниже, чтобы авторизоваться в Linear.\n\n"
                "После авторизации вы сможете видеть свои задачи прямо в боте.",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except ValueError as e:
            await message.answer(
                f"❌ Ошибка конфигурации: {e}\n\n"
                "Обратитесь к администратору."
            )


@router.callback_query(F.data == "linear_tasks")
async def callback_linear_tasks(callback: CallbackQuery):
    """Callback для кнопки 'Мои задачи' Linear."""
    try:
        from clients.linear_oauth import linear_oauth
    except ImportError:
        await callback.answer("Linear интеграция не настроена", show_alert=True)
        return

    telegram_user_id = callback.from_user.id

    if not linear_oauth.is_connected(telegram_user_id):
        await callback.answer("Linear не подключён", show_alert=True)
        return

    await callback.answer()  # Убираем loading state

    issues = await linear_oauth.get_my_issues(telegram_user_id, limit=10)

    if not issues:
        await callback.message.answer("📋 В Linear пока нет задач.")
        return

    lines = ["📋 *Задачи Linear:*\n"]
    for issue in issues:
        state_name = issue.get("state", {}).get("name", "?")
        identifier = issue.get("identifier", "?")
        title = issue.get("title", "Без названия")
        url = issue.get("url", "")

        lines.append(f"• [{identifier}]({url}) — {title}\n  _{state_name}_")

    await callback.message.answer("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


@router.callback_query(F.data == "linear_disconnect")
async def callback_linear_disconnect(callback: CallbackQuery):
    """Callback для кнопки 'Отключить Linear'."""
    try:
        from clients.linear_oauth import linear_oauth
    except ImportError:
        await callback.answer("Linear интеграция не настроена", show_alert=True)
        return

    telegram_user_id = callback.from_user.id

    if not linear_oauth.is_connected(telegram_user_id):
        await callback.answer("Linear уже отключён", show_alert=True)
        return

    linear_oauth.disconnect(telegram_user_id)
    await callback.answer("Linear отключён", show_alert=True)

    # Обновляем сообщение
    await callback.message.edit_text(
        "🔌 *Linear отключён*\n\n"
        "Используйте /linear чтобы подключиться снова.",
        parse_mode="Markdown"
    )


@router.message(Command("language"))
async def cmd_language(message: Message, state: FSMContext):
    """Команда смены языка напрямую"""
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    await message.answer(
        t('settings.language.title', lang),
        reply_markup=kb_language_select()
    )
    await state.set_state(UpdateStates.choosing_field)


# --- Обновление профиля ---

@router.message(Command("update"))
async def cmd_update(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    if not intern or not intern.get('onboarding_completed'):
        await message.answer(t('errors.try_again', lang) + " /start")
        return

    # State Machine routing
    if state_machine is not None:
        logger.info(f"[SM] /update command routed to StateMachine for chat_id={message.chat.id}")
        try:
            await state.clear()  # Очищаем legacy FSM state перед SM routing
            await state_machine.go_to(intern, "common.settings")
            return
        except Exception as e:
            logger.error(f"[SM] Error routing /update to StateMachine: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Fallback to legacy

    study_duration = intern['study_duration']
    bloom_level = intern['bloom_level']
    bloom_emojis = {1: '🔵', 2: '🟡', 3: '🔴'}

    # Получаем дату старта марафона
    start_date = intern.get('marathon_start_date')
    if start_date:
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        marathon_start_str = start_date.strftime('%d.%m.%Y')
    else:
        marathon_start_str = "—"

    marathon_day = get_marathon_day(intern)

    interests_str = ', '.join(intern['interests']) if intern['interests'] else '—'
    motivation_short = intern.get('motivation', '')[:80] + '...' if len(intern.get('motivation', '')) > 80 else intern.get('motivation', '') or '—'
    goals_short = intern['goals'][:80] + '...' if len(intern['goals']) > 80 else intern['goals'] or '—'

    await message.answer(
        f"👤 *{intern['name']}*\n"
        f"💼 {intern.get('occupation', '') or '—'}\n"
        f"🎨 {interests_str}\n\n"
        f"💫 {motivation_short}\n"
        f"🎯 {goals_short}\n\n"
        f"{t(f'duration.minutes_{study_duration}', lang)}\n"
        f"{bloom_emojis.get(bloom_level, '🔵')} {t(f'bloom.level_{bloom_level}_short', lang)}\n"
        f"🗓 {marathon_start_str} ({t('progress.day', lang, day=marathon_day, total=14)})\n"
        f"⏰ {intern['schedule_time']}\n"
        f"🌐 {get_language_name(lang)}\n\n"
        f"*{t('settings.what_to_change', lang)}*",
        parse_mode="Markdown",
        reply_markup=kb_update_profile(lang)
    )
    await state.set_state(UpdateStates.choosing_field)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_name")
async def on_upd_name(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        f"👤 *{t('update.your_name', lang)}:* {intern['name']}\n\n"
        f"{t('update.whats_your_name', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_name)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_occupation")
async def on_upd_occupation(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        f"💼 *{t('update.your_occupation', lang)}:* {intern.get('occupation', '') or t('profile.not_specified', lang)}\n\n"
        f"{t('update.whats_your_occupation', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_occupation)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_interests")
async def on_upd_interests(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    interests_str = ', '.join(intern['interests']) if intern['interests'] else t('profile.not_specified', lang)
    await callback.answer()
    await callback.message.edit_text(
        f"🎨 *{t('update.your_interests', lang)}:* {interests_str}\n\n"
        f"{t('update.what_interests', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_interests)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_motivation")
async def on_upd_motivation(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    await callback.answer()
    await callback.message.edit_text(
        f"💫 *Что сейчас важно:*\n{intern.get('motivation', '') or 'не указано'}\n\n"
        "Что для вас по-настоящему важно в жизни?",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_motivation)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_goals")
async def on_upd_goals(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        f"🎯 *{t('update.your_goals', lang)}:*\n{intern['goals'] or t('profile.not_specified', lang)}\n\n"
        f"{t('update.what_goals', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_goals)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_duration")
async def on_upd_duration(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    await callback.answer()
    await callback.message.edit_text(
        f"⏱ *{t('update.current_time', lang)}:* {duration.get('emoji', '')} {duration.get('name', '')}\n\n"
        f"{t('update.how_many_minutes', lang)}",
        parse_mode="Markdown",
        reply_markup=kb_study_duration(lang)
    )
    await state.set_state(UpdateStates.updating_duration)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_schedule")
async def on_upd_schedule(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        f"⏰ *{t('update.current_schedule', lang)}:* {intern['schedule_time']}\n\n"
        f"{t('update.when_remind', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_schedule)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_bloom")
async def on_upd_bloom(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    level = intern['bloom_level']
    emojis = {1: '🔵', 2: '🟡', 3: '🔴'}
    await callback.answer()
    await callback.message.edit_text(
        f"🎚 *{t('update.current_difficulty', lang)}:* {emojis.get(level, '🔵')} {t(f'bloom.level_{level}_short', lang)}\n"
        f"_{t(f'bloom.level_{level}_desc', lang)}_\n\n"
        f"📊 *{t('update.difficulty_scale', lang)}:* 1 — {t('update.easiest', lang)}, 3 — {t('update.hardest', lang)}\n\n"
        f"{t('update.select_difficulty', lang)}",
        parse_mode="Markdown",
        reply_markup=kb_bloom_level(lang)
    )
    await state.set_state(UpdateStates.updating_bloom_level)

@router.callback_query(UpdateStates.updating_bloom_level, F.data.startswith("bloom_"))
async def on_save_bloom(callback: CallbackQuery, state: FSMContext):
    level = int(callback.data.replace("bloom_", ""))
    await update_intern(callback.message.chat.id, bloom_level=level, topics_at_current_bloom=0)

    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    await callback.answer(f"{t(f'bloom.level_{level}_short', lang)}")
    await callback.message.edit_text(
        f"✅ {t('update.difficulty_changed', lang)}: *{t(f'bloom.level_{level}_short', lang)}*!\n\n"
        f"{t(f'bloom.level_{level}_desc', lang)}\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.mode', lang)}\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_mode")
async def on_upd_mode(callback: CallbackQuery, state: FSMContext):
    """Переход к выбору режима (Марафон/Лента)"""
    await state.clear()
    await callback.answer()

    # Импортируем функцию выбора режима
    try:
        from engines.mode_selector import cmd_mode
        # Создаём фейковое сообщение для вызова команды
        await cmd_mode(callback.message)
    except ImportError:
        await callback.message.edit_text(
            "🎯 *Выбор режима*\n\n"
            "Используйте команду /mode для выбора режима работы.",
            parse_mode="Markdown"
        )


@router.callback_query(UpdateStates.choosing_field, F.data == "upd_marathon_start")
async def on_upd_marathon_start(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    start_date = intern.get('marathon_start_date')
    marathon_day = get_marathon_day(intern)

    if start_date:
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        current_date_str = start_date.strftime('%d.%m.%Y')
    else:
        current_date_str = "не задана"

    await callback.answer()
    await callback.message.edit_text(
        f"🗓 *Текущая дата старта:* {current_date_str}\n"
        f"*День марафона:* {marathon_day} из {MARATHON_DAYS}\n\n"
        f"⚠️ *Внимание:* изменение даты старта влияет на расчёт текущего дня марафона.\n\n"
        f"Выберите новую дату старта:",
        parse_mode="Markdown",
        reply_markup=kb_marathon_start()
    )
    await state.set_state(UpdateStates.updating_marathon_start)

@router.callback_query(UpdateStates.updating_marathon_start, F.data.startswith("start_"))
async def on_save_marathon_start(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    today = moscow_today()

    if callback.data == "start_today":
        start_date = today
        date_text = t('update.today', lang)
    elif callback.data == "start_tomorrow":
        start_date = today + timedelta(days=1)
        date_text = t('update.tomorrow', lang)
    else:  # start_day_after
        start_date = today + timedelta(days=2)
        date_text = t('update.day_after_tomorrow', lang)

    await update_intern(callback.message.chat.id, marathon_start_date=start_date)

    await callback.answer(t('update.start_date_updated', lang))
    await callback.message.edit_text(
        f"✅ {t('update.marathon_start_changed', lang)}\n\n"
        f"{t('update.new_date', lang)}: *{start_date.strftime('%d.%m.%Y')}* ({date_text})\n\n"
        f"{t('update.continue_learning_hint', lang)}\n"
        f"{t('update.update_more', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_language")
async def on_upd_language(callback: CallbackQuery, state: FSMContext):
    """Показать меню выбора языка"""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        t('settings.language.title', lang),
        reply_markup=kb_language_select()
    )

@router.callback_query(UpdateStates.choosing_field, F.data.startswith("lang_"))
async def on_select_language(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор языка"""
    new_lang = callback.data.replace("lang_", "")
    if new_lang not in SUPPORTED_LANGUAGES:
        new_lang = 'ru'

    await update_intern(callback.message.chat.id, language=new_lang)
    await callback.answer(t('settings.language.changed', new_lang))
    await callback.message.edit_text(
        t('settings.language.changed', new_lang) + "\n\n" +
        t('commands.learn', new_lang) + "\n" +
        t('commands.update', new_lang)
    )
    await state.clear()

@router.message(UpdateStates.updating_motivation)
async def on_save_motivation(message: Message, state: FSMContext):
    await update_intern(message.chat.id, motivation=message.text.strip())
    await message.answer(
        "✅ Обновлено!\n\n"
        "Теперь мотивационные блоки будут ещё точнее.\n\n"
        "/learn — продолжить обучение\n"
        "/update — изменить ещё что-то"
    )
    await state.clear()

@router.message(UpdateStates.updating_goals)
async def on_save_goals(message: Message, state: FSMContext):
    await update_intern(message.chat.id, goals=message.text.strip())
    await message.answer(
        "✅ Обновлено!\n\n"
        "Теперь материалы будут персонализированы под ваши цели.\n\n"
        "/learn — продолжить обучение\n"
        "/update — изменить ещё что-то"
    )
    await state.clear()

@router.message(UpdateStates.updating_name)
async def on_save_name(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')
    await update_intern(message.chat.id, name=message.text.strip())
    await message.answer(
        f"✅ {t('update.name_changed', lang)}: *{message.text.strip()}*\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()

@router.message(UpdateStates.updating_occupation)
async def on_save_occupation(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')
    await update_intern(message.chat.id, occupation=message.text.strip())
    await message.answer(
        f"✅ {t('update.occupation_changed', lang)}!\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}"
    )
    await state.clear()

@router.message(UpdateStates.updating_interests)
async def on_save_interests(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')
    interests = [i.strip() for i in message.text.replace(',', ';').split(';') if i.strip()]
    await update_intern(message.chat.id, interests=interests)
    await message.answer(
        f"✅ {t('update.interests_changed', lang)}!\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}"
    )
    await state.clear()

@router.callback_query(UpdateStates.updating_duration, F.data.startswith("duration_"))
async def on_save_duration(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    duration = int(callback.data.replace("duration_", ""))
    await update_intern(callback.message.chat.id, study_duration=duration)
    duration_info = STUDY_DURATIONS.get(str(duration), {})
    await callback.answer(t('update.saved', lang))
    await callback.message.edit_text(
        f"✅ {t('update.duration_changed', lang)}: {duration_info.get('emoji', '')} *{duration_info.get('name', '')}*\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()

@router.message(UpdateStates.updating_schedule)
async def on_save_schedule(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')
    try:
        h, m = map(int, message.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except:
        await message.answer(t('errors.try_again', lang) + " (HH:MM)")
        return

    # Нормализуем формат времени (с ведущими нулями)
    normalized_time = f"{h:02d}:{m:02d}"
    await update_intern(message.chat.id, schedule_time=normalized_time)
    await message.answer(
        f"✅ {t('update.schedule_changed', lang)}: *{normalized_time}*\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()

@router.message(LearningStates.waiting_for_answer)
async def on_answer(message: Message, state: FSMContext, bot: Bot):
    chat_id = message.chat.id
    text = message.text or ''
    current_state = await state.get_state()
    logger.info(f"[on_answer] ВЫЗВАН для chat_id={chat_id}, state={current_state}, text={text[:50] if text else '[no text]'}")
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    # Bypass: если State Machine включён и у пользователя есть SM-состояние
    if state_machine is not None and intern and intern.get('current_state'):
        logger.info(f"[on_answer] Bypassing legacy handler, SM state: {intern.get('current_state')}")
        await state.clear()  # Очищаем legacy FSM state
        await state_machine.handle(intern, message)
        return

    # Проверяем, это вопрос к ИИ (начинается с ?)
    if text.strip().startswith('?'):
        question_text = text.strip()[1:].strip()
        if question_text:
            # Обрабатываем как вопрос, оставаясь в текущем состоянии
            progress_msg = await message.answer(t('loading.progress.analyzing', lang))
            try:
                answer, sources = await handle_question(
                    question=question_text,
                    intern=intern,
                    context_topic=get_topic(intern['current_topic_index']),
                    progress_callback=None
                )
                response = answer
                if sources:
                    response += "\n\n📚 _Источники: " + ", ".join(sources[:2]) + "_"
                await progress_msg.delete()
                await message.answer(
                    response + f"\n\n💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.answer_expected', lang)}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Ошибка при обработке вопроса: {e}")
                await progress_msg.delete()
                await message.answer(t('errors.try_again', lang))
            # Проверяем, что состояние сохранилось
            final_state = await state.get_state()
            logger.info(f"[on_answer] После обработки вопроса, state={final_state} для chat_id={chat_id}")
            return  # Остаёмся в состоянии waiting_for_answer

    if len(text.strip()) < 20:
        await message.answer(t('marathon.write_more_details', lang))
        return

    # Сохраняем ответ
    await save_answer(message.chat.id, intern['current_topic_index'], text.strip())

    # Обновляем прогресс и счётчик тем на текущем уровне Блума
    completed = intern['completed_topics'] + [intern['current_topic_index']]
    topics_at_bloom = intern['topics_at_current_bloom'] + 1
    bloom_level = intern['bloom_level']

    # Автоматическое повышение уровня после N тем
    level_upgraded = False
    if topics_at_bloom >= BLOOM_AUTO_UPGRADE_AFTER and bloom_level < 3:
        bloom_level += 1
        topics_at_bloom = 0
        level_upgraded = True

    # Обновляем счётчик тем за сегодня
    today = moscow_today()
    topics_today = get_topics_today(intern) + 1

    await update_intern(
        message.chat.id,
        completed_topics=completed,
        current_topic_index=intern['current_topic_index'] + 1,
        bloom_level=bloom_level,
        topics_at_current_bloom=topics_at_bloom,
        topics_today=topics_today,
        last_topic_date=today
    )

    done = len(completed)
    total = get_total_topics()
    lang = intern.get('language', 'ru')

    # Сообщение о повышении уровня
    upgrade_msg = ""
    if level_upgraded:
        upgrade_msg = f"\n\n🎉 *{t('marathon.level_up', lang)}* *{t(f'bloom.level_{bloom_level}_short', lang)}*!"

    # Получаем информацию о следующей доступной теме
    updated_intern = {
        **intern,
        'completed_topics': completed,
        'current_topic_index': intern['current_topic_index'] + 1,
        'topics_today': topics_today,
        'last_topic_date': today
    }
    next_available = get_available_topics(updated_intern)
    next_topic_hint = ""
    next_command = t('marathon.next_command', lang)
    if next_available:
        next_topic = next_available[0][1]  # (index, topic) -> topic
        # Определяем тип следующей темы
        if next_topic.get('type') == 'practice':
            next_topic_hint = f"\n\n📝 *{t('marathon.next_task', lang)}:* {get_topic_title(next_topic, lang)}"
            next_command = t('marathon.continue_to_task', lang)
        else:
            next_topic_hint = f"\n\n📚 *{t('marathon.next_lesson', lang)}:* {get_topic_title(next_topic, lang)}"
            next_command = t('marathon.continue_to_lesson', lang)

    # Если уровень ниже максимального — предлагаем дополнительный вопрос
    if intern['bloom_level'] < 3:
        # Сохраняем индекс темы в state для бонусного вопроса
        await state.update_data(topic_index=intern['current_topic_index'], next_command=next_command)

        await message.answer(
            f"✅ *{t('marathon.topic_completed', lang)}*\n\n"
            f"{progress_bar(done, total)}\n"
            f"{t(f'bloom.level_{bloom_level}_short', lang)}{upgrade_msg}{next_topic_hint}\n\n"
            f"{t('marathon.want_harder', lang)}",
            parse_mode="Markdown",
            reply_markup=kb_bonus_question(lang)
        )
        # Не очищаем state — ждём выбора
    else:
        # Уровень максимальный, бонус не предлагаем — сразу к заданию
        # FIX: Получаем практику того же дня, что и завершённая теория
        completed_topic = TOPICS[intern['current_topic_index']]
        practice = get_practice_for_day(updated_intern, completed_topic['day'])

        if practice:
            practice_index, practice_topic = practice
            await message.answer(
                f"✅ *{t('marathon.topic_completed', lang)}*\n\n"
                f"{progress_bar(done, total)}\n"
                f"{t(f'bloom.level_{bloom_level}_short', lang)}{upgrade_msg}\n\n"
                f"⏳ {t('marathon.loading_practice', lang)}",
                parse_mode="Markdown"
            )
            # Обновляем current_topic_index
            await update_intern(chat_id, current_topic_index=practice_index)
            # Отправляем задание
            await send_practice_topic(chat_id, practice_topic, updated_intern, state, bot)
        else:
            # День завершён
            await message.answer(
                f"✅ *{t('marathon.topic_completed', lang)}*\n\n"
                f"{progress_bar(done, total)}\n"
                f"{t(f'bloom.level_{bloom_level}_short', lang)}{upgrade_msg}\n\n"
                f"✅ {t('marathon.day_complete', lang)}",
                parse_mode="Markdown"
            )
            await state.clear()

@router.callback_query(F.data == "bonus_yes")
async def on_bonus_yes(callback: CallbackQuery, state: FSMContext):
    """Пользователь хочет дополнительный вопрос посложнее"""
    await callback.answer()
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    logger.info(f"[BONUS] on_bonus_yes вызван для chat_id={chat_id}, user_id={user_id}")

    data = await state.get_data()
    topic_index = data.get('topic_index', 0)
    next_command = data.get('next_command')
    logger.info(f"[BONUS] State data: topic_index={topic_index}, next_command={next_command}")

    intern = await get_intern(chat_id)
    topic = get_topic(topic_index)
    lang = intern.get('language', 'ru') if intern else 'ru'

    if not topic:
        await callback.message.edit_text(f"Не удалось найти тему.\n\n{next_command or t('marathon.next_command', lang)}")
        await state.clear()
        return

    await callback.message.edit_text(f"⏳ {t('marathon.generating_harder', lang)}")

    try:
        # Генерируем вопрос следующего уровня
        marathon_day = get_marathon_day(intern)
        next_level = min(intern['bloom_level'] + 1, 3)
        logger.info(f"[BONUS] Генерируем вопрос уровня {next_level} для темы {topic_index}")
        question = await claude.generate_question(topic, intern, bloom_level=next_level)

        # ВАЖНО: Устанавливаем состояние СРАЗУ после генерации вопроса, ДО отправки
        await state.update_data(topic_index=topic_index, next_command=next_command, bonus_level=next_level)
        await state.set_state(LearningStates.waiting_for_bonus_answer)
        current_state = await state.get_state()
        logger.info(f"[BONUS] Состояние установлено ДО отправки сообщения: {current_state}")

        # Теперь отправляем сообщение
        await callback.message.answer(
            f"🚀 *{t('marathon.bonus_question', lang)}* ({t(f'bloom.level_{next_level}_short', lang)})\n\n"
            f"{question}\n\n"
            f"{t('marathon.write_answer', lang)}",
            parse_mode="Markdown"
        )

        # Финальная проверка состояния
        final_state = await state.get_state()
        logger.info(f"[BONUS] Состояние после отправки сообщения: {final_state}")
    except Exception as e:
        logger.error(f"Ошибка генерации бонусного вопроса: {e}")
        import traceback
        logger.error(f"[BONUS] Traceback: {traceback.format_exc()}")
        # Если не удалось сгенерировать вопрос — предлагаем продолжить
        await callback.message.answer(
            f"Не удалось сгенерировать бонусный вопрос. Попробуйте позже.\n\n"
            f"{next_command or t('marathon.next_command', lang)}"
        )
        await state.clear()

@router.callback_query(F.data == "bonus_no")
async def on_bonus_no(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Пользователь отказался от дополнительного вопроса → переход к заданию"""
    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    data = await state.get_data()
    next_command = data.get('next_command', t('marathon.next_command', lang))
    await callback.answer(t('marathon.ok', lang))

    # Проверяем, есть ли практика для этого дня
    # FIX: Используем сохранённый индекс темы для определения дня практики
    topic_index = data.get('topic_index', 0)
    completed_topic = TOPICS[topic_index] if topic_index < len(TOPICS) else None
    practice = get_practice_for_day(intern, completed_topic['day']) if completed_topic else None

    if practice:
        practice_index, practice_topic = practice
        await callback.message.edit_text(
            callback.message.text + f"\n\n⏳ {t('marathon.loading_practice', lang)}",
            parse_mode="Markdown"
        )
        # Обновляем current_topic_index
        await update_intern(chat_id, current_topic_index=practice_index)
        # Отправляем задание
        await send_practice_topic(chat_id, practice_topic, intern, state, bot)
    else:
        # День завершён (нет практики или уже выполнена)
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ {t('marathon.day_complete', lang)}",
            parse_mode="Markdown"
        )
        await state.clear()

@router.message(LearningStates.waiting_for_bonus_answer)
async def on_bonus_answer(message: Message, state: FSMContext, bot: Bot):
    """Обработка ответа на бонусный вопрос → переход к заданию"""
    chat_id = message.chat.id
    text = message.text or ''
    current_state = await state.get_state()
    logger.info(f"[BONUS] on_bonus_answer вызван для chat_id={chat_id}, state={current_state}")

    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    # Bypass: если State Machine включён и у пользователя есть SM-состояние
    if state_machine is not None and intern and intern.get('current_state'):
        logger.info(f"[on_bonus_answer] Bypassing legacy handler, SM state: {intern.get('current_state')}")
        await state.clear()  # Очищаем legacy FSM state
        await state_machine.handle(intern, message)
        return

    # Проверяем, это вопрос к ИИ (начинается с ?)
    if text.strip().startswith('?'):
        question_text = text.strip()[1:].strip()
        if question_text:
            data = await state.get_data()
            topic_index = data.get('topic_index', 0)
            # Обрабатываем как вопрос, оставаясь в текущем состоянии
            progress_msg = await message.answer(t('loading.progress.analyzing', lang))
            try:
                answer, sources = await handle_question(
                    question=question_text,
                    intern=intern,
                    context_topic=get_topic(topic_index),
                    progress_callback=None
                )
                response = answer
                if sources:
                    response += "\n\n📚 _Источники: " + ", ".join(sources[:2]) + "_"
                await progress_msg.delete()
                await message.answer(
                    response + f"\n\n💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.answer_expected', lang)}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Ошибка при обработке вопроса: {e}")
                await progress_msg.delete()
                await message.answer(t('errors.try_again', lang))
            return  # Остаёмся в состоянии waiting_for_bonus_answer

    if len(text.strip()) < 20:
        await message.answer(t('marathon.write_more_details', lang))
        return

    # intern и lang уже получены выше
    data = await state.get_data()
    topic_index = data.get('topic_index', 0)
    logger.info(f"[BONUS] Processing answer: topic_index={topic_index}, data_keys={list(data.keys())}")

    try:
        # Сохраняем ответ на бонусный вопрос
        await save_answer(chat_id, topic_index, f"[BONUS] {text.strip()}")

        bloom_level = intern['bloom_level'] if intern else 1

        # Проверяем, есть ли практика для этого дня
        # FIX: Используем сохранённый индекс темы для определения дня практики
        completed_topic = TOPICS[topic_index] if topic_index < len(TOPICS) else None
        practice = get_practice_for_day(intern, completed_topic['day']) if completed_topic else None

        if practice:
            practice_index, practice_topic = practice
            await message.answer(
                f"🌟 *{t('marathon.bonus_completed', lang)}*\n\n"
                f"{t('marathon.training_skills', lang)} *{t(f'bloom.level_{bloom_level}_short', lang)}* {t('marathon.and_higher', lang)}\n\n"
                f"⏳ {t('marathon.loading_practice', lang)}",
                parse_mode="Markdown"
            )
            # Обновляем current_topic_index
            await update_intern(chat_id, current_topic_index=practice_index)
            # Отправляем задание
            await send_practice_topic(chat_id, practice_topic, intern, state, bot)
        else:
            # День завершён
            await message.answer(
                f"🌟 *{t('marathon.bonus_completed', lang)}*\n\n"
                f"{t('marathon.training_skills', lang)} *{t(f'bloom.level_{bloom_level}_short', lang)}* {t('marathon.and_higher', lang)}\n\n"
                f"✅ {t('marathon.day_complete', lang)}",
                parse_mode="Markdown"
            )
            await state.clear()
    except Exception as e:
        logger.error(f"Ошибка обработки бонусного ответа: {e}")
        await message.answer(f"✅ {t('marathon.answer_accepted', lang)}\n\n{t('marathon.next_command', lang)}")
        await state.clear()

@router.callback_query(LearningStates.waiting_for_answer, F.data == "skip_topic")
async def on_skip_topic(callback: CallbackQuery, state: FSMContext):
    """Пропуск теоретической темы без ответа"""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    next_index = intern['current_topic_index'] + 1
    await update_intern(callback.message.chat.id, current_topic_index=next_index)

    topic = get_topic(intern['current_topic_index'])
    topic_title = get_topic_title(topic, lang) if topic else t('marathon.topic_default', lang)

    await callback.answer(t('marathon.topic_skipped', lang))
    await callback.message.edit_text(
        t('marathon.topic_skipped_message', lang, title=topic_title),
        parse_mode="Markdown"
    )
    await state.clear()


@router.message(LearningStates.waiting_for_work_product)
async def on_work_product(message: Message, state: FSMContext):
    """Обработка отправки рабочего продукта"""
    text = message.text or ''
    chat_id = message.chat.id
    current_state = await state.get_state()
    logger.info(f"[on_work_product] ВЫЗВАН для chat_id={chat_id}, state={current_state}, text={text[:50] if text else '[no text]'}")
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    # Bypass: если State Machine включён и у пользователя есть SM-состояние
    if state_machine is not None and intern and intern.get('current_state'):
        logger.info(f"[on_work_product] Bypassing legacy handler, SM state: {intern.get('current_state')}")
        await state.clear()  # Очищаем legacy FSM state
        await state_machine.handle(intern, message)
        return

    # Проверяем, это вопрос к ИИ (начинается с ?)
    if text.strip().startswith('?'):
        question_text = text.strip()[1:].strip()
        if question_text:
            # Обрабатываем как вопрос, оставаясь в текущем состоянии
            progress_msg = await message.answer(t('loading.progress.analyzing', lang))
            try:
                answer, sources = await handle_question(
                    question=question_text,
                    intern=intern,
                    context_topic=get_topic(intern['current_topic_index']),
                    progress_callback=None
                )
                response = answer
                if sources:
                    response += "\n\n📚 _Источники: " + ", ".join(sources[:2]) + "_"
                await progress_msg.delete()
                await message.answer(
                    response + f"\n\n💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.work_product_name', lang)}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Ошибка при обработке вопроса: {e}")
                await progress_msg.delete()
                await message.answer(t('errors.try_again', lang))
            return  # Остаёмся в состоянии waiting_for_work_product

    if len(text.strip()) < 3:
        await message.answer(f"{t('marathon.write_wp_minimum', lang)} ({t('marathon.wp_example_hint', lang)})")
        return

    # Сохраняем ответ (рабочий продукт)
    topic_index = intern['current_topic_index']
    await save_answer(
        message.chat.id,
        topic_index,
        f"[РП] {text.strip()}"
    )

    # Получаем информацию о завершённой теме
    topic = get_topic(topic_index)
    topic_day = topic['day'] if topic else get_marathon_day(intern)

    # Обновляем прогресс
    completed = intern['completed_topics'] + [topic_index]

    # Обновляем счётчик тем за сегодня
    today = moscow_today()
    topics_today = get_topics_today(intern) + 1

    await update_intern(
        message.chat.id,
        completed_topics=completed,
        current_topic_index=topic_index + 1,
        topics_today=topics_today,
        last_topic_date=today
    )

    done = len(completed)
    total = get_total_topics()

    # Проверяем, завершён ли день ЗАВЕРШЁННОЙ темы (не текущий день марафона!)
    day_topics = get_topics_for_day(topic_day)
    day_completed = sum(1 for i, _ in enumerate(TOPICS) if TOPICS[i]['day'] == topic_day and i in completed)

    if day_completed >= len(day_topics):
        # День темы полностью завершён
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📊 {t('buttons.view_progress', lang)}", callback_data="go_progress")]
        ])
        await message.answer(
            f"🎉 *{t('marathon.day_completed_title', lang, day=topic_day)}*\n\n"
            f"✅ {t('marathon.day_completed_theory', lang)}\n"
            f"✅ {t('marathon.day_completed_practice', lang)}\n"
            f"📝 {t('marathon.day_completed_wp', lang, work_product=text.strip())}\n\n"
            f"{progress_bar(done, total)}\n\n"
            f"{t('marathon.day_completed_great', lang)}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📚 {t('buttons.next_topic_btn', lang)}", callback_data="learn")]
        ])
        await message.answer(
            f"✅ *{t('marathon.practice_accepted', lang)}*\n\n"
            f"📝 {t('marathon.day_completed_wp', lang, work_product=text.strip())}\n\n"
            f"{progress_bar(done, total)}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    await state.clear()


@router.callback_query(LearningStates.waiting_for_work_product, F.data == "skip_practice")
async def on_skip_practice(callback: CallbackQuery, state: FSMContext):
    """Пропуск практической темы"""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    next_index = intern['current_topic_index'] + 1
    await update_intern(callback.message.chat.id, current_topic_index=next_index)

    topic = get_topic(intern['current_topic_index'])
    topic_title = get_topic_title(topic, lang) if topic else t('marathon.practice_default', lang)

    await callback.answer(t('marathon.practice_skipped', lang))
    await callback.message.edit_text(
        t('marathon.practice_skipped_message', lang, title=topic_title),
        parse_mode="Markdown"
    )
    await state.clear()

# --- Отправка темы ---

async def send_topic(chat_id: int, state: Optional[FSMContext], bot: Bot):
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    marathon_day = get_marathon_day(intern)

    # Автоматический запуск марафона при первом /learn
    if marathon_day == 0:
        start_date = intern.get('marathon_start_date')
        if start_date:
            # Дата старта в будущем
            await bot.send_message(
                chat_id,
                f"🗓 {t('marathon.marathon_not_started', lang)}\n\n"
                f"{t('marathon.marathon_starts', lang, date=start_date.strftime('%d.%m.%Y'))}\n\n"
                f"{t('update.update_more', lang)}",
                parse_mode="Markdown"
            )
            return
        else:
            # Автоматически запускаем марафон сегодня
            today = moscow_today()
            await update_intern(chat_id, marathon_start_date=today)
            await bot.send_message(
                chat_id,
                f"🚀 *{t('marathon.marathon_launched', lang)}*\n\n"
                f"{t('marathon.marathon_starts', lang, date=today.strftime('%d.%m.%Y'))} ({t('update.today', lang)})\n\n"
                f"{t('update.update_more', lang)}",
                parse_mode="Markdown"
            )
            # Обновляем данные
            intern = await get_intern(chat_id)
            marathon_day = get_marathon_day(intern)

    # Проверяем дневной лимит
    topics_today = get_topics_today(intern)
    if topics_today >= MAX_TOPICS_PER_DAY:
        await bot.send_message(
            chat_id,
            f"🎯 *{t('marathon.daily_limit_title', lang, count=topics_today)}*\n\n"
            f"{t('marathon.daily_limit_info', lang, max=MAX_TOPICS_PER_DAY)}\n\n"
            f"{t('marathon.daily_limit_motto', lang)}\n\n"
            f"{t('marathon.daily_limit_return', lang, time=intern['schedule_time'])}",
            parse_mode="Markdown"
        )
        return

    # Получаем следующую тему
    topic_index = get_next_topic_index(intern)
    topic = get_topic(topic_index) if topic_index is not None else None

    if topic_index is not None and topic_index != intern['current_topic_index']:
        await update_intern(chat_id, current_topic_index=topic_index)

    if not topic:
        total_topics = get_total_topics()
        completed_count = len(intern['completed_topics'])

        if total_topics == 0:
            logger.error(f"TOPICS is empty! Cannot send topic to {chat_id}")
            await bot.send_message(
                chat_id,
                "⚠️ *Технические неполадки*\n\n"
                "Структура обучения временно недоступна.",
                parse_mode="Markdown"
            )
            return

        # Проверяем, все ли темы пройдены или ждём следующий день
        available = get_available_topics(intern)
        if not available and completed_count < total_topics:
            # Темы за сегодня закончились, ждём следующий день
            await bot.send_message(
                chat_id,
                f"✅ *{t('marathon.day_completed', lang, day=marathon_day)}*\n\n"
                f"{t('marathon.topics_passed_of_total', lang, completed=completed_count, total=total_topics)}\n\n"
                f"{t('marathon.next_topics_tomorrow', lang)}\n"
                f"{t('marathon.return_at', lang, time=intern['schedule_time'])}",
                parse_mode="Markdown"
            )
            return

        if completed_count >= total_topics:
            # Марафон пройден — короткое сообщение (пользователь сам запросил /learn)
            progress = get_lessons_tasks_progress(intern['completed_topics'])

            await bot.send_message(
                chat_id,
                f"🎉 *{t('marathon.congratulations_completed', lang)}*\n\n"
                f"{t('marathon.completed_all_days', lang, days=MARATHON_DAYS, topics=total_topics)}\n\n"
                f"📊 *{t('marathon.your_statistics', lang)}:*\n"
                f"📖 {t('progress.lessons', lang)}: {progress['lessons']['completed']}/{progress['lessons']['total']}\n"
                f"📝 {t('progress.tasks', lang)}: {progress['tasks']['completed']}/{progress['tasks']['total']}\n\n"
                f"{t('marathon.workshop_link', lang)}",
                parse_mode="Markdown"
            )
            return

        await bot.send_message(
            chat_id,
            f"⚠️ {t('marathon.something_wrong', lang)}",
            parse_mode="Markdown"
        )
        return

    # Отправляем тему в зависимости от типа
    topic_type = topic.get('type', 'theory')

    if topic_type == 'theory':
        await send_theory_topic(chat_id, topic, intern, state, bot)
    else:
        await send_practice_topic(chat_id, topic, intern, state, bot)


async def send_theory_topic(chat_id: int, topic: dict, intern: dict, state: Optional[FSMContext], bot: Bot):
    """Отправка теоретической темы"""
    marathon_day = get_marathon_day(intern)
    topic_day = topic.get('day', marathon_day)
    lang = intern.get('language', 'ru')
    bloom_level = intern['bloom_level']

    # Показываем, что бот работает
    await bot.send_chat_action(chat_id=chat_id, action="typing")
    await bot.send_message(chat_id, f"⏳ {t('marathon.generating_material', lang)}")

    # Генерируем контент с таймаутом
    content = None
    try:
        content = await asyncio.wait_for(
            claude.generate_content(topic, intern, mcp_client=mcp_guides, knowledge_client=mcp_knowledge),
            timeout=60.0  # 60 секунд для контента
        )
    except asyncio.TimeoutError:
        logger.error(f"[send_theory_topic] Таймаут генерации контента для chat_id={chat_id}, topic={topic.get('title')}")
    except Exception as e:
        logger.error(f"[send_theory_topic] Ошибка генерации контента: {e}")

    if not content:
        await bot.send_message(
            chat_id,
            f"❌ {t('errors.content_generation_failed', lang)}\n\n"
            f"{t('errors.try_again_later', lang)}",
            parse_mode="Markdown"
        )
        return

    # Генерируем вопрос с таймаутом
    question = None
    try:
        question = await asyncio.wait_for(
            claude.generate_question(topic, intern),
            timeout=30.0  # 30 секунд для вопроса
        )
    except asyncio.TimeoutError:
        logger.warning(f"[send_theory_topic] Таймаут генерации вопроса для chat_id={chat_id}")
    except Exception as e:
        logger.error(f"[send_theory_topic] Ошибка генерации вопроса: {e}")

    # Fallback вопрос если генерация не удалась
    if not question:
        question = t('marathon.fallback_question', lang, topic=topic.get('title', 'тема'))

    # Используем день из темы, а не текущий день марафона
    header = (
        f"📚 *{t('marathon.day_theory', lang, day=topic_day)}*\n"
        f"*{get_topic_title(topic, lang)}*\n"
        f"⏱ {t('marathon.minutes', lang, minutes=intern['study_duration'])}\n\n"
    )

    full = header + content
    if len(full) > 4000:
        await bot.send_message(chat_id, header, parse_mode="Markdown")
        for i in range(0, len(content), 4000):
            await bot.send_message(chat_id, content[i:i+4000])
    else:
        await bot.send_message(chat_id, full, parse_mode="Markdown")

    # ВАЖНО: Устанавливаем состояние ДО отправки сообщения
    # чтобы избежать гонки, когда пользователь отвечает быстрее, чем сохраняется состояние
    if state:
        await state.set_state(LearningStates.waiting_for_answer)

    # Вопрос отдельным сообщением с подсказкой о состоянии
    await bot.send_message(
        chat_id,
        f"💭 *{t('marathon.reflection_question', lang)}* ({t(f'bloom.level_{bloom_level}_short', lang)})\n\n"
        f"{question}\n\n"
        f"_{t('marathon.answer_hint', lang)}_\n\n"
        f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.answer_expected', lang)}\n"
        f"_{t('marathon.question_hint', lang)}_",
        parse_mode="Markdown",
        reply_markup=kb_skip_topic(lang)
    )


async def send_practice_topic(chat_id: int, topic: dict, intern: dict, state: Optional[FSMContext], bot: Bot):
    """Отправка практической темы"""
    marathon_day = get_marathon_day(intern)
    topic_day = topic.get('day', marathon_day)
    lang = intern.get('language', 'ru')

    # Показываем, что бот работает
    await bot.send_chat_action(chat_id=chat_id, action="typing")
    await bot.send_message(chat_id, f"⏳ {t('marathon.preparing_practice', lang)}")

    # Генерируем краткое введение (с таймаутом и обработкой ошибок)
    intro = ""
    try:
        intro = await asyncio.wait_for(
            claude.generate_practice_intro(topic, intern),
            timeout=30.0  # 30 секунд таймаут
        )
    except asyncio.TimeoutError:
        logger.warning(f"[send_practice_topic] Таймаут генерации intro для chat_id={chat_id}")
    except Exception as e:
        logger.error(f"[send_practice_topic] Ошибка генерации intro: {e}")

    task = topic.get('task', '')
    work_product = topic.get('work_product', '')
    examples = topic.get('work_product_examples', [])

    examples_text = ""
    if examples:
        examples_text = f"\n*{t('marathon.wp_examples', lang)}:*\n" + "\n".join([f"• {ex}" for ex in examples])

    # Используем день из темы, а не текущий день марафона
    header = (
        f"✏️ *{t('marathon.day_practice', lang, day=topic_day)}*\n"
        f"*{get_topic_title(topic, lang)}*\n\n"
    )

    content = f"{intro}\n\n" if intro else ""
    content += f"📋 *{t('marathon.task', lang)}:*\n{task}\n\n"
    content += f"🎯 *{t('marathon.work_product', lang)}:* {work_product}"
    content += examples_text

    full = header + content
    if len(full) > 4000:
        await bot.send_message(chat_id, header, parse_mode="Markdown")
        await bot.send_message(chat_id, content, parse_mode="Markdown")
    else:
        await bot.send_message(chat_id, full, parse_mode="Markdown")

    # ВАЖНО: Устанавливаем состояние ДО отправки сообщения
    # чтобы избежать гонки, когда пользователь отвечает быстрее, чем сохраняется состояние
    if state:
        await state.set_state(LearningStates.waiting_for_work_product)

    # Запрос рабочего продукта с подсказкой о состоянии
    await bot.send_message(
        chat_id,
        f"📝 *{t('marathon.when_complete', lang)}:*\n\n"
        f"{t('marathon.write_wp_name', lang)}\n\n"
        f"_{t('marathon.example', lang)}: «{examples[0] if examples else work_product}»_\n\n"
        f"_{t('marathon.no_check_hint', lang)}_\n\n"
        f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.work_product_name', lang)}\n"
        f"_{t('marathon.question_hint', lang)}_",
        parse_mode="Markdown",
        reply_markup=kb_submit_work_product(lang)
    )

# ============= ПЛАНИРОВЩИК =============

scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)

# Глобальный dispatcher для доступа к FSM storage
_dispatcher: Optional[Dispatcher] = None

# State Machine (инициализируется в main() если USE_STATE_MACHINE=true)
state_machine = None

async def send_scheduled_topic(chat_id: int, bot: Bot):
    """Отправка темы по расписанию"""
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    marathon_day = get_marathon_day(intern)

    # Проверяем, начался ли марафон
    if marathon_day == 0:
        logger.info(f"[Scheduler] {chat_id}: marathon_day=0, пропуск (марафон не начался)")
        return  # Марафон ещё не начался

    # Проверяем дневной лимит
    topics_today = get_topics_today(intern)
    if topics_today >= MAX_TOPICS_PER_DAY:
        logger.info(f"[Scheduler] {chat_id}: topics_today={topics_today}, пропуск (лимит)")
        return  # Лимит достигнут

    # Получаем следующую тему
    topic_index = get_next_topic_index(intern)
    topic = get_topic(topic_index) if topic_index is not None else None

    if not topic:
        # Проверяем, все ли темы пройдены
        total = get_total_topics()
        completed_count = len(intern['completed_topics'])
        if completed_count >= total:
            # Марафон пройден — полное сообщение (автоматическая отправка по расписанию)
            progress = get_lessons_tasks_progress(intern['completed_topics'])

            await bot.send_message(
                chat_id,
                f"🎉 *{t('marathon.congratulations_completed', lang)}*\n\n"
                f"{t('marathon.completed_all_days', lang, days=MARATHON_DAYS, topics=total)}\n\n"
                f"📊 *{t('marathon.your_statistics', lang)}:*\n"
                f"📖 {t('progress.lessons', lang)}: {progress['lessons']['completed']}/{progress['lessons']['total']}\n"
                f"📝 {t('progress.tasks', lang)}: {progress['tasks']['completed']}/{progress['tasks']['total']}\n\n"
                f"{t('marathon.now_practicing_learner', lang)}:\n"
                f"{t('marathon.practices_list', lang)}\n\n"
                f"{t('marathon.want_continue', lang)}\n"
                f"{t('marathon.workshop_full_link', lang)}",
                parse_mode="Markdown"
            )
        return

    if topic_index is not None and topic_index != intern['current_topic_index']:
        await update_intern(chat_id, current_topic_index=topic_index)

    # Планируем напоминания (+1ч и +3ч)
    await schedule_reminders(chat_id, intern)

    # Отправляем тему
    topic_type = topic.get('type', 'theory')

    # === State Machine routing ===
    # Если SM включён — направляем через State Machine
    if state_machine is not None:
        logger.info(f"[Scheduler] Routing to StateMachine: chat_id={chat_id}, topic_type={topic_type}")
        try:
            # Определяем целевой стейт
            if topic_type == 'theory':
                target_state = "workshop.marathon.lesson"
            else:
                target_state = "workshop.marathon.task"

            # Очищаем legacy FSM state перед SM routing
            if _dispatcher:
                fsm_ctx = FSMContext(
                    storage=_dispatcher.storage,
                    key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id)
                )
                await fsm_ctx.clear()

            # Обновляем intern с актуальным topic_index перед переходом
            updated_intern = {**intern, 'current_topic_index': topic_index}

            await state_machine.go_to(updated_intern, target_state, context={'topic_index': topic_index, 'from_scheduler': True})
            logger.info(f"[Scheduler] Successfully routed to {target_state} for chat_id={chat_id}")
            return
        except Exception as e:
            logger.error(f"[Scheduler] Error routing to StateMachine: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Fallback на legacy функции

    # === Legacy routing ===
    if _dispatcher:
        state = FSMContext(
            storage=_dispatcher.storage,
            key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id)
        )

        if topic_type == 'theory':
            await send_theory_topic(chat_id, topic, intern, state, bot)
        else:
            await send_practice_topic(chat_id, topic, intern, state, bot)


async def schedule_reminders(chat_id: int, intern: dict):
    """Планирует напоминания для пользователя"""
    now = moscow_now()

    # Добавляем записи о напоминаниях в БД
    async with (await get_pool()).acquire() as conn:
        # Удаляем старые неотправленные напоминания
        await conn.execute(
            'DELETE FROM reminders WHERE chat_id = $1 AND sent = FALSE',
            chat_id
        )

        # Планируем напоминания +1ч и +3ч
        for hours in [1, 3]:
            reminder_time = now + timedelta(hours=hours)
            # Убираем timezone для совместимости с TIMESTAMP (без timezone)
            reminder_time_naive = reminder_time.replace(tzinfo=None)
            await conn.execute(
                '''INSERT INTO reminders (chat_id, reminder_type, scheduled_for)
                   VALUES ($1, $2, $3)''',
                chat_id, f'+{hours}h', reminder_time_naive
            )


async def send_reminder(chat_id: int, reminder_type: str, bot: Bot):
    """Отправляет напоминание"""
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    topics_today = get_topics_today(intern)

    # Если уже начал изучение сегодня — не напоминаем
    if topics_today > 0:
        return

    marathon_day = get_marathon_day(intern)
    if marathon_day == 0:
        return

    if reminder_type == '+1h':
        await bot.send_message(
            chat_id,
            f"⏰ *{t('reminders.title', lang)}*\n\n"
            f"{t('reminders.day_waiting', lang, day=marathon_day)}\n\n"
            f"{t('reminders.two_topics_today', lang)}\n\n"
            f"{t('reminders.start_command', lang)}",
            parse_mode="Markdown"
        )
    elif reminder_type == '+3h':
        await bot.send_message(
            chat_id,
            f"🔔 *{t('reminders.last_reminder', lang)}*\n\n"
            f"{t('reminders.day_not_started', lang, day=marathon_day)}\n\n"
            f"{t('reminders.regularity_tip', lang)}\n"
            f"{t('reminders.even_15_min', lang)}\n\n"
            f"{t('reminders.start_command', lang)}",
            parse_mode="Markdown"
        )


async def check_reminders():
    """Проверяет и отправляет запланированные напоминания"""
    now = moscow_now()
    # Убираем timezone для совместимости с TIMESTAMP (без timezone)
    now_naive = now.replace(tzinfo=None)

    async with (await get_pool()).acquire() as conn:
        # Получаем напоминания, которые пора отправить
        rows = await conn.fetch(
            '''SELECT id, chat_id, reminder_type FROM reminders
               WHERE sent = FALSE AND scheduled_for <= $1''',
            now_naive
        )

        if not rows:
            return

        bot = Bot(token=BOT_TOKEN)

        for row in rows:
            try:
                await send_reminder(row['chat_id'], row['reminder_type'], bot)
                await conn.execute(
                    'UPDATE reminders SET sent = TRUE WHERE id = $1',
                    row['id']
                )
                logger.info(f"Sent {row['reminder_type']} reminder to {row['chat_id']}")
            except Exception as e:
                error_msg = str(e).lower()
                if 'blocked' in error_msg or 'deactivated' in error_msg:
                    # Пользователь заблокировал бота — помечаем reminder как отправленный
                    logger.warning(f"User {row['chat_id']} blocked bot, marking reminder {row['id']} as sent")
                    await conn.execute(
                        'UPDATE reminders SET sent = TRUE WHERE id = $1',
                        row['id']
                    )
                else:
                    logger.error(f"Failed to send reminder to {row['chat_id']}: {e}")

        await bot.session.close()


async def scheduled_check():
    """Проверка расписания каждую минуту"""
    now = moscow_now()
    time_str = f"{now.hour:02d}:{now.minute:02d}"

    # Логируем каждые 10 минут для подтверждения работы scheduler
    if now.minute % 10 == 0:
        logger.info(f"[Scheduler] Проверка в {time_str} MSK")

    chat_ids = await get_all_scheduled_interns(now.hour, now.minute)

    if chat_ids:
        logger.info(f"[Scheduler] {time_str} MSK — найдено {len(chat_ids)} пользователей для отправки")
        bot = Bot(token=BOT_TOKEN)
        me = await bot.get_me()  # Инициализируем bot.id для FSMContext
        logger.info(f"[Scheduler] Bot ID: {bot.id}, username: {me.username}")
        for chat_id in chat_ids:
            try:
                await send_scheduled_topic(chat_id, bot)
                logger.info(f"[Scheduler] Отправлена тема пользователю {chat_id}")
            except Exception as e:
                error_msg = str(e).lower()
                if 'blocked' in error_msg or 'deactivated' in error_msg:
                    logger.warning(f"[Scheduler] User {chat_id} blocked bot, skipping")
                else:
                    logger.error(f"[Scheduler] Ошибка отправки пользователю {chat_id}: {e}")
        await bot.session.close()

    # Проверяем напоминания
    await check_reminders()

# ============= FALLBACK HANDLERS =============

# Фильтр для исключения callback'ов, обрабатываемых другими роутерами
def is_main_router_callback(callback: CallbackQuery) -> bool:
    """Проверяет, что callback НЕ принадлежит engines/ роутерам"""
    if not callback.data:
        return True
    # Исключаем callback'и, которые обрабатываются mode_router и feed_router
    excluded_prefixes = ('mode_', 'feed_')
    return not callback.data.startswith(excluded_prefixes)

@router.callback_query(is_main_router_callback)
async def on_unknown_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка неизвестных callback-запросов (истёкшие кнопки и т.д.)"""
    logger.warning(f"Unhandled callback: {callback.data} from user {callback.from_user.id}")
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    await callback.answer(
        t('fsm.button_expired', lang),
        show_alert=True
    )

@router.message()
async def on_unknown_message(message: Message, state: FSMContext):
    """Обработка сообщений вне FSM-состояний"""
    chat_id = message.chat.id
    text = message.text or ''

    # === STATE MACHINE ROUTING ===
    # Если StateMachine включён — направляем туда
    if state_machine is not None:
        logger.info(f"[SM] Routing message to StateMachine: chat_id={chat_id}, text={text[:50] if text else '[no text]'}")
        try:
            # Очищаем legacy FSM state чтобы избежать конфликтов
            await state.clear()

            intern = await get_intern(chat_id)
            if intern:
                await state_machine.handle(intern, message)
                return
            else:
                # Новый пользователь — запускаем онбординг через SM
                await state_machine.start({'telegram_id': chat_id}, context={'message': message})
                return
        except Exception as e:
            logger.error(f"[SM] Error in StateMachine: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # НЕ делаем fallback на legacy — показываем ошибку пользователю
            intern = await get_intern(chat_id)
            lang = intern.get('language', 'ru') if intern else 'ru'
            await message.answer(
                f"⚠️ {t('errors.processing_error', lang)}\n\n"
                f"{t('errors.try_again_later', lang)}"
            )
            return

    # === LEGACY ROUTING (старая логика) ===
    current_state = await state.get_state()
    logger.info(f"[UNKNOWN] on_unknown_message вызван для chat_id={chat_id}, state={current_state}, text={text[:50] if text else '[no text]'}")

    # Если пользователь в каком-то состоянии — пробуем обработать вручную
    if current_state:
        logger.warning(f"[UNKNOWN] Message in state {current_state} reached fallback. Attempting manual routing for chat_id={chat_id}")
        # Загружаем данные пользователя для локализации сообщений
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') if intern else 'ru'
        logger.info(f"[UNKNOWN] Expected states: answer={LearningStates.waiting_for_answer.state}, work={LearningStates.waiting_for_work_product.state}, bonus={LearningStates.waiting_for_bonus_answer.state}")

        try:
            # Маршрутизируем на существующие хэндлеры
            if current_state == LearningStates.waiting_for_answer.state:
                logger.info(f"[UNKNOWN] Routing to on_answer for chat_id={chat_id}")
                await on_answer(message, state, message.bot)
                return
            elif current_state == LearningStates.waiting_for_work_product.state:
                logger.info(f"[UNKNOWN] Routing to on_work_product for chat_id={chat_id}")
                await on_work_product(message, state)
                return
            elif current_state == LearningStates.waiting_for_bonus_answer.state:
                logger.info(f"[UNKNOWN] Routing to on_bonus_answer for chat_id={chat_id}")
                await on_bonus_answer(message, state, message.bot)
                return
        except Exception as e:
            logger.error(f"[UNKNOWN] Error routing to handler: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await message.answer(t('fsm.error_try_learn', lang))
            return

        # Для других состояний — показываем подсказку
        # Но сначала проверим, не консультация ли это (начинается с ?)
        text = message.text or ''
        if text.startswith('?') and state_machine is not None:
            # Роутим в консультацию через State Machine
            intern = await get_intern(chat_id)
            if intern:
                await state.clear()  # Очищаем FSM состояние
                await state_machine.go_to(intern, "common.consultation", context={'question': text[1:].strip()})
                return

        if 'OnboardingStates' in current_state:
            await message.answer(t('fsm.unrecognized_onboarding', lang))
            return
        elif 'UpdateStates' in current_state:
            await message.answer(t('fsm.unrecognized_update', lang))
            return
        elif 'FeedStates' in current_state:
            await message.answer(t('fsm.unrecognized_feed', lang))
            return
        elif 'MarathonSettingsStates' in current_state:
            await message.answer(t('fsm.enter_time_format', lang))
            return

        # Неизвестное состояние — показываем команды
        logger.warning(f"[UNKNOWN] Unknown state {current_state} for chat_id={chat_id}")
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(
            f"Состояние: {current_state}\n\n"
            f"{t('commands.learn', lang)}\n"
            f"{t('commands.progress', lang)}\n"
            f"{t('commands.help', lang)}"
        )
        return

    # Пользователь не в FSM-состоянии
    logger.info(f"[UNKNOWN] Пользователь {chat_id} не в FSM-состоянии, проверяем intent")
    intern = await get_intern(chat_id)

    if not intern:
        # Новый пользователь — определяем язык из Telegram
        lang = detect_language(message.from_user.language_code if message.from_user else None)
        await message.answer(t('fsm.new_user_start', lang))
        return

    lang = intern.get('language', 'ru') or 'ru'

    # Проверяем, начинается ли сообщение с "?" — явный вопрос к ИИ
    is_explicit_question = text.strip().startswith('?')
    question_text = text.strip()[1:].strip() if is_explicit_question else text

    # Fallback для режима марафона (восстановление после потери FSM state)
    if intern.get('mode') == 'marathon' and intern.get('onboarding_completed') and not is_explicit_question:
        # 1. Проверяем, есть ли незавершённый урок (теория была отправлена, ответ не получен)
        theory = has_pending_theory(intern)
        if theory and was_theory_sent_today(intern):
            theory_index, theory_topic = theory
            # Проверяем, что это не команда и достаточно длинное сообщение
            if text and not text.startswith('/') and len(text.strip()) >= 20:
                logger.info(f"[Fallback] Accepting message as theory answer for user {chat_id}, theory {theory_index}")

                # Сохраняем ответ
                await save_answer(chat_id, theory_index, f"[fallback] {text.strip()}")

                # Обновляем прогресс
                completed = intern['completed_topics'] + [theory_index]
                topics_at_bloom = intern['topics_at_current_bloom'] + 1
                bloom_level = intern['bloom_level']

                # Автоматическое повышение уровня
                level_upgraded = False
                if topics_at_bloom >= BLOOM_AUTO_UPGRADE_AFTER and bloom_level < 3:
                    bloom_level += 1
                    topics_at_bloom = 0
                    level_upgraded = True

                today = moscow_today()
                topics_today = get_topics_today(intern) + 1

                await update_intern(
                    chat_id,
                    completed_topics=completed,
                    current_topic_index=theory_index + 1,
                    bloom_level=bloom_level,
                    topics_at_current_bloom=topics_at_bloom,
                    topics_today=topics_today,
                    last_topic_date=today
                )

                done = len(completed)
                total = get_total_topics()

                upgrade_msg = ""
                if level_upgraded:
                    upgrade_msg = f"\n\n🎉 *{t('marathon.level_up', lang)}* *{t(f'bloom.level_{bloom_level}_short', lang)}*!"

                # Проверяем, есть ли практика для этого дня
                # FIX: Используем день завершённой теории для поиска практики
                updated_intern = {**intern, 'completed_topics': completed}
                practice = get_practice_for_day(updated_intern, theory_topic['day'])

                if practice:
                    practice_index, practice_topic = practice
                    await message.answer(
                        f"✅ *{t('marathon.topic_completed', lang)}*{upgrade_msg}\n\n"
                        f"{progress_bar(done, total)}\n\n"
                        f"⏳ {t('marathon.loading_practice', lang)}",
                        parse_mode="Markdown"
                    )
                    # Обновляем current_topic_index и отправляем практику
                    await update_intern(chat_id, current_topic_index=practice_index)
                    # Нет state для FSM в fallback — практика будет принята через fallback практики
                    task_text = practice_topic.get('task', '')
                    work_product = practice_topic.get('work_product', '')
                    await message.answer(
                        f"📝 *{t('marathon.day_practice', lang, day=practice_topic.get('day', ''))}*\n"
                        f"*{get_topic_title(practice_topic, lang)}*\n\n"
                        f"📋 *{t('marathon.task', lang)}:*\n{task_text}\n\n"
                        f"🎯 *{t('marathon.work_product', lang)}:* {work_product}\n\n"
                        f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.work_product_name', lang)}\n"
                        f"_{t('marathon.question_hint', lang)}_",
                        parse_mode="Markdown"
                    )
                else:
                    await message.answer(
                        f"✅ *{t('marathon.topic_completed', lang)}*{upgrade_msg}\n\n"
                        f"{progress_bar(done, total)}\n\n"
                        f"✅ {t('marathon.day_complete', lang)}",
                        parse_mode="Markdown"
                    )
                return

        # 2. Проверяем, есть ли незавершённая практика (теория пройдена)
        practice = has_pending_practice(intern)
        if practice:
            practice_index, practice_topic = practice
            # Проверяем, что это не команда и не короткое сообщение
            if text and not text.startswith('/') and len(text.strip()) >= 3:
                # Проверяем, прошла ли теория этого дня (дня ПРАКТИКИ, не текущий день марафона)
                practice_day = practice_topic.get('day', get_marathon_day(intern))
                day_topics = [(i, t) for i, t in enumerate(TOPICS) if t['day'] == practice_day]
                theory_done = any(
                    i in intern['completed_topics']
                    for i, t in day_topics if t.get('type') == 'theory'
                )

                if theory_done:
                    # Теория пройдена, практика ждёт ответа — принимаем как рабочий продукт
                    logger.info(f"[Fallback] Accepting message as work product for user {chat_id}, practice {practice_index}")

                    # Сохраняем ответ (рабочий продукт)
                    await save_answer(
                        chat_id,
                        practice_index,
                        f"[РП][fallback] {text.strip()}"
                    )

                    # Обновляем прогресс
                    completed = intern['completed_topics'] + [practice_index]
                    today = moscow_today()
                    topics_today = get_topics_today(intern) + 1

                    await update_intern(
                        chat_id,
                        completed_topics=completed,
                        current_topic_index=practice_index + 1,
                        topics_today=topics_today,
                        last_topic_date=today
                    )

                    done = len(completed)
                    total = get_total_topics()

                    await message.answer(
                        f"✅ *{t('marathon.practice_accepted', lang)}*\n\n"
                        f"📝 РП: {text.strip()[:100]}{'...' if len(text.strip()) > 100 else ''}\n\n"
                        f"{progress_bar(done, total)}\n\n"
                        f"✅ {t('marathon.day_complete', lang)}",
                        parse_mode="Markdown"
                    )
                    return

    # Определяем намерение пользователя
    # Если начинается с "?" — это явный вопрос, иначе используем detect_intent
    if is_explicit_question:
        intent_is_question = True
    else:
        intent = detect_intent(text, context={'mode': intern.get('mode')})
        intent_is_question = intent.type == IntentType.QUESTION

    if intent_is_question:
        # Пользователь задаёт вопрос — отвечаем через Claude + MCP
        # Отправляем начальное сообщение о прогрессе
        progress_msg = await message.answer(t('loading.progress.analyzing', lang))

        # Создаём callback для обновления прогресса
        async def update_progress(stage: str, percent: int):
            """Обновляет сообщение о прогрессе"""
            stage_texts = {
                ProcessingStage.ANALYZING: t('loading.progress.analyzing', lang),
                ProcessingStage.SEARCHING: t('loading.progress.searching', lang),
                ProcessingStage.GENERATING: t('loading.progress.generating', lang),
                ProcessingStage.DONE: t('loading.progress.done', lang),
            }
            new_text = stage_texts.get(stage, t('loading.processing', lang))
            try:
                await progress_msg.edit_text(new_text)
            except Exception:
                pass  # Игнорируем ошибки редактирования (например, текст не изменился)

        try:
            # Используем question_text (без "?" если был явный вопрос)
            answer, sources = await handle_question(
                question=question_text if is_explicit_question else text,
                intern=intern,
                context_topic=None,
                progress_callback=update_progress
            )

            response = answer
            if sources:
                response += "\n\n📚 _Источники: " + ", ".join(sources[:2]) + "_"

            # Удаляем сообщение о прогрессе и отправляем ответ
            try:
                await progress_msg.delete()
            except Exception:
                pass
            await message.answer(response, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка при обработке вопроса: {e}")
            try:
                await progress_msg.delete()
            except Exception:
                pass
            await message.answer(t('errors.try_again', lang))

    elif not is_explicit_question and intent.type == IntentType.TOPIC_REQUEST:
        # Пользователь хочет тему — перенаправляем на /learn
        await message.answer(
            "Для получения темы используйте /learn"
        )

    else:
        # Не распознано — показываем команды
        await message.answer(
            t('commands.learn', lang) + "\n" +
            t('commands.progress', lang) + "\n" +
            t('commands.profile', lang) + "\n" +
            t('commands.help', lang)
        )

# ============= ЗАПУСК =============

async def main():
    global _dispatcher, state_machine

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
            # db и llm пока None — они инициализируются позже
            # Стейты будут использовать глобальные функции из db/queries

            register_all_states(
                machine=state_machine,
                bot=bot,
                db=None,  # Используем глобальные db функции
                llm=None,  # Используем глобальный Claude client
                i18n=i18n
            )

            logger.info(f"✅ StateMachine инициализирован ({len(state_machine._states)} стейтов)")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации StateMachine: {e}")
            import traceback
            traceback.print_exc()
            state_machine = None
    dp = Dispatcher(storage=PostgresStorage())

    # Регистрируем middleware для логирования
    dp.message.middleware(LoggingMiddleware())

    # Подключаем роутеры режимов ПЕРЕД основным роутером
    # (чтобы catch-all handler в router не перехватывал их callback'и)
    try:
        from engines.integration import setup_routers
        setup_routers(dp)
    except ImportError as e:
        logger.warning(f"⚠️ Не удалось загрузить engines: {e}. Режимы Лента и выбор режима недоступны.")

    # Основной роутер подключаем последним
    dp.include_router(router)

    # Сохраняем dispatcher для доступа к FSM storage из планировщика
    _dispatcher = dp

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
    scheduler.add_job(scheduled_check, 'cron', minute='*')
    scheduler.start()

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
