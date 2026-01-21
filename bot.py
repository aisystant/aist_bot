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

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import aiohttp
import asyncpg

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

# ============= БАЗА ДАННЫХ =============

db_pool: Optional[asyncpg.Pool] = None

async def init_db():
    """Инициализация базы данных"""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS interns (
                chat_id BIGINT PRIMARY KEY,
                name TEXT DEFAULT '',
                role TEXT DEFAULT '',
                domain TEXT DEFAULT '',
                interests TEXT DEFAULT '[]',
                experience_level TEXT DEFAULT '',
                difficulty_preference TEXT DEFAULT '',
                learning_style TEXT DEFAULT '',
                study_duration INTEGER DEFAULT 15,
                current_problems TEXT DEFAULT '',
                desires TEXT DEFAULT '',
                goals TEXT DEFAULT '',
                schedule_time TEXT DEFAULT '09:00',
                current_topic_index INTEGER DEFAULT 0,
                completed_topics TEXT DEFAULT '[]',
                bloom_level INTEGER DEFAULT 1,
                topics_at_current_bloom INTEGER DEFAULT 0,
                topics_today INTEGER DEFAULT 0,
                last_topic_date DATE DEFAULT NULL,
                onboarding_completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Миграции
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS study_duration INTEGER DEFAULT 15')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS current_problems TEXT DEFAULT \'\'')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS desires TEXT DEFAULT \'\'')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS bloom_level INTEGER DEFAULT 1')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS topics_at_current_bloom INTEGER DEFAULT 0')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS topics_today INTEGER DEFAULT 0')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS last_topic_date DATE DEFAULT NULL')
        # Новые поля для упрощённого онбординга
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS occupation TEXT DEFAULT \'\'')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS motivation TEXT DEFAULT \'\'')
        # Порядок тем: default, by_interests, hybrid
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS topic_order TEXT DEFAULT \'default\'')
        # Марафон: дата старта
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS marathon_start_date DATE DEFAULT NULL')

        # Режимы работы (Марафон/Лента)
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT \'marathon\'')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS marathon_status TEXT DEFAULT \'not_started\'')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS marathon_paused_at DATE DEFAULT NULL')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS feed_status TEXT DEFAULT \'not_started\'')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS feed_started_at DATE DEFAULT NULL')

        # Систематичность
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS active_days_total INTEGER DEFAULT 0')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS active_days_streak INTEGER DEFAULT 0')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS longest_streak INTEGER DEFAULT 0')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS last_active_date DATE DEFAULT NULL')

        # Сложность (новое название для bloom)
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS complexity_level INTEGER DEFAULT 1')
        await conn.execute('ALTER TABLE interns ADD COLUMN IF NOT EXISTS topics_at_current_complexity INTEGER DEFAULT 0')

        # Таблица для напоминаний
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                reminder_type TEXT,
                scheduled_for TIMESTAMP,
                sent BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS answers (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                topic_index INTEGER,
                answer TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Лента: недельные планы
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS feed_weeks (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                week_number INTEGER,
                week_start DATE,
                suggested_topics TEXT DEFAULT '[]',
                accepted_topics TEXT DEFAULT '[]',
                current_day INTEGER DEFAULT 0,
                status TEXT DEFAULT 'planning',
                ended_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Лента: сессии
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS feed_sessions (
                id SERIAL PRIMARY KEY,
                week_id INTEGER,
                day_number INTEGER,
                topic_title TEXT,
                content TEXT DEFAULT '{}',
                session_date DATE,
                status TEXT DEFAULT 'active',
                fixation_text TEXT,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Лог активности
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                activity_date DATE,
                activity_type TEXT,
                mode TEXT DEFAULT 'marathon',
                reference_id INTEGER,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(chat_id, activity_date, activity_type)
            )
        ''')

    logger.info("✅ База данных инициализирована")

async def get_intern(chat_id: int) -> dict:
    """Получить профиль стажера из БД"""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM interns WHERE chat_id = $1', chat_id
        )
        
        if row:
            return {
                'chat_id': row['chat_id'],
                'name': row['name'],
                'occupation': row['occupation'] if 'occupation' in row.keys() else '',
                'role': row['role'],
                'domain': row['domain'],
                'interests': json.loads(row['interests']),
                'motivation': row['motivation'] if 'motivation' in row.keys() else '',
                'experience_level': row['experience_level'],
                'difficulty_preference': row['difficulty_preference'],
                'learning_style': row['learning_style'],
                'study_duration': row['study_duration'],
                'current_problems': row['current_problems'] or '',
                'desires': row['desires'] or '',
                'goals': row['goals'],
                'schedule_time': row['schedule_time'],
                'current_topic_index': row['current_topic_index'],
                'completed_topics': json.loads(row['completed_topics']),
                'bloom_level': row['bloom_level'] if row['bloom_level'] else 1,
                'topics_at_current_bloom': row['topics_at_current_bloom'] if row['topics_at_current_bloom'] else 0,
                'topics_today': row['topics_today'] if row['topics_today'] else 0,
                'last_topic_date': row['last_topic_date'],
                'topic_order': row['topic_order'] if 'topic_order' in row.keys() else 'default',
                'marathon_start_date': row['marathon_start_date'] if 'marathon_start_date' in row.keys() else None,
                'onboarding_completed': row['onboarding_completed']
            }
        else:
            # Создаём нового пользователя
            await conn.execute(
                'INSERT INTO interns (chat_id) VALUES ($1) ON CONFLICT DO NOTHING',
                chat_id
            )
            return {
                'chat_id': chat_id,
                'name': '',
                'occupation': '',
                'role': '',
                'domain': '',
                'interests': [],
                'motivation': '',
                'experience_level': '',
                'difficulty_preference': '',
                'learning_style': '',
                'study_duration': 15,
                'current_problems': '',
                'desires': '',
                'goals': '',
                'schedule_time': '09:00',
                'current_topic_index': 0,
                'completed_topics': [],
                'bloom_level': 1,
                'topics_at_current_bloom': 0,
                'topics_today': 0,
                'last_topic_date': None,
                'topic_order': 'default',
                'marathon_start_date': None,
                'onboarding_completed': False
            }

async def update_intern(chat_id: int, **kwargs):
    """Обновить данные стажера"""
    async with db_pool.acquire() as conn:
        for key, value in kwargs.items():
            if key in ['interests', 'completed_topics']:
                value = json.dumps(value)
            await conn.execute(
                f'UPDATE interns SET {key} = $1, updated_at = NOW() WHERE chat_id = $2',
                value, chat_id
            )

async def save_answer(chat_id: int, topic_index: int, answer: str):
    """Сохранить ответ стажера"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO answers (chat_id, topic_index, answer) VALUES ($1, $2, $3)',
            chat_id, topic_index, answer
        )

async def get_all_scheduled_interns(hour: int, minute: int) -> list:
    """Получить всех стажеров с заданным временем обучения"""
    time_str = f"{hour:02d}:{minute:02d}"
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT chat_id FROM interns WHERE schedule_time = $1 AND onboarding_completed = TRUE',
            time_str
        )
        return [row['chat_id'] for row in rows]

def get_topics_today(intern: dict) -> int:
    """Получить количество тем, пройденных сегодня"""
    today = moscow_today()
    last_date = intern.get('last_topic_date')

    # Если last_topic_date — это дата сегодня, возвращаем topics_today
    if last_date and last_date == today:
        return intern.get('topics_today', 0)
    # Иначе — новый день, счётчик обнуляется
    return 0

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

class ClaudeClient:
    def __init__(self):
        self.api_key = ANTHROPIC_API_KEY
        self.base_url = "https://api.anthropic.com/v1/messages"

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        async with aiohttp.ClientSession() as session:
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01"
            }
            
            payload = {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}]
            }
            
            try:
                async with session.post(self.base_url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["content"][0]["text"]
                    else:
                        error = await resp.text()
                        logger.error(f"Claude API error: {error}")
                        return None
            except Exception as e:
                logger.error(f"Claude API exception: {e}")
                return None

    async def generate_content(self, topic: dict, intern: dict, marathon_day: int = 1, mcp_client=None, knowledge_client=None) -> str:
        """Генерирует контент для теоретической темы марафона

        Args:
            topic: тема для генерации
            intern: профиль стажера
            marathon_day: день марафона для ротации примеров
            mcp_client: клиент MCP для руководств (guides)
            knowledge_client: клиент MCP для базы знаний (knowledge) - приоритет свежим постам
        """
        duration = STUDY_DURATIONS.get(str(intern['study_duration']), {"words": 1500})
        words = duration.get('words', 1500)

        # Пробуем загрузить метаданные темы для точных поисковых запросов
        topic_id = topic.get('id', '')
        metadata = load_topic_metadata(topic_id) if topic_id else None

        # Используем ключи поиска из метаданных или формируем общий запрос
        if metadata:
            guides_search_keys = get_search_keys(metadata, "guides_mcp")
            knowledge_search_keys = get_search_keys(metadata, "knowledge_mcp")
            logger.info(f"Загружены метаданные темы {topic_id}: {len(guides_search_keys)} guides, {len(knowledge_search_keys)} knowledge")
        else:
            # Fallback на общий запрос
            default_query = f"{topic.get('title')} {topic.get('main_concept')}"
            guides_search_keys = [default_query]
            knowledge_search_keys = [default_query]

        # Получаем контекст из MCP руководств (используем все ключи поиска)
        guides_context = ""
        if mcp_client:
            try:
                context_parts = []
                seen_texts = set()  # Для дедупликации
                for search_query in guides_search_keys[:3]:  # Максимум 3 запроса
                    search_results = await mcp_client.semantic_search(search_query, lang="ru", limit=2)
                    if search_results:
                        for item in search_results:
                            if isinstance(item, dict):
                                text = item.get('text', item.get('content', ''))
                            elif isinstance(item, str):
                                text = item
                            else:
                                continue
                            if text and text[:100] not in seen_texts:
                                seen_texts.add(text[:100])
                                context_parts.append(text[:1500])
                if context_parts:
                    guides_context = "\n\n".join(context_parts[:5])  # Максимум 5 фрагментов
                    logger.info(f"{mcp_client.name}: найдено {len(context_parts)} фрагментов контекста")
            except Exception as e:
                logger.error(f"{mcp_client.name} search error: {e}")

        # Получаем контекст из MCP базы знаний (knowledge MCP использует инструмент 'search')
        knowledge_context = ""
        if knowledge_client:
            try:
                context_parts = []
                seen_texts = set()
                for search_query in knowledge_search_keys[:3]:  # Максимум 3 запроса
                    # Сортируем по дате создания (сначала новые)
                    search_results = await knowledge_client.semantic_search(
                        search_query, lang="ru", limit=2, sort_by="created_at:desc"
                    )
                    if search_results:
                        for item in search_results:
                            if isinstance(item, dict):
                                text = item.get('text', item.get('content', ''))
                                date_info = item.get('created_at', item.get('date', ''))
                                if date_info:
                                    text = f"[{date_info}] {text}"
                            elif isinstance(item, str):
                                text = item
                            else:
                                continue
                            if text and text[:100] not in seen_texts:
                                seen_texts.add(text[:100])
                                context_parts.append(text[:1500])
                if context_parts:
                    knowledge_context = "\n\n".join(context_parts[:5])  # Максимум 5 фрагментов
                    logger.info(f"{knowledge_client.name}: найдено {len(context_parts)} фрагментов (свежие посты)")
            except Exception as e:
                logger.error(f"{knowledge_client.name} search error: {e}")

        # Объединяем контексты (knowledge имеет приоритет, поэтому идёт первым)
        mcp_context = ""
        if knowledge_context and guides_context:
            mcp_context = f"АКТУАЛЬНЫЕ ПОСТЫ:\n{knowledge_context}\n\n---\n\nИЗ РУКОВОДСТВ:\n{guides_context}"
        elif knowledge_context:
            mcp_context = knowledge_context
        elif guides_context:
            mcp_context = guides_context

        # Используем content_prompt из структуры знаний, если есть
        content_prompt = topic.get('content_prompt', '')

        # Определяем тип контекста для промпта
        has_both = knowledge_context and guides_context
        context_instruction = ""
        if has_both:
            context_instruction = "Используй предоставленный контекст: актуальные посты имеют приоритет, руководства дополняют."
        elif mcp_context:
            context_instruction = "Используй предоставленный контекст из материалов Aisystant как основу."

        system_prompt = f"""Ты — персональный наставник по системному мышлению и личному развитию.
{get_personalization_prompt(intern, marathon_day)}

Создай текст на {intern['study_duration']} минут чтения (~{words} слов). Без заголовков, только абзацы.
Текст должен быть вовлекающим, с примерами из жизни читателя.

СТРОГО ЗАПРЕЩЕНО:
- Добавлять вопросы в любом месте текста
- Использовать заголовки типа "Вопрос:", "Вопрос для размышления:", "Вопрос для проверки:" и т.п.
- Заканчивать текст вопросом
Вопрос будет задан отдельно после текста.
{context_instruction}"""

        pain_point = topic.get('pain_point', '')
        key_insight = topic.get('key_insight', '')
        source = topic.get('source', '')

        user_prompt = f"""Тема: {topic.get('title')}
Основное понятие: {topic.get('main_concept')}
Связанные понятия: {', '.join(topic.get('related_concepts', []))}

{"Боль читателя: " + pain_point if pain_point else ""}
{"Ключевой инсайт: " + key_insight if key_insight else ""}
{"Источник: " + source if source else ""}

{f"ИНСТРУКЦИЯ ПО КОНТЕНТУ:{chr(10)}{content_prompt}" if content_prompt else ""}

{f"КОНТЕКСТ ИЗ МАТЕРИАЛОВ AISYSTANT:{chr(10)}{mcp_context}" if mcp_context else ""}

Начни с признания боли читателя, затем раскрой тему и подведи к ключевому инсайту.
{"Опирайся на контекст, но адаптируй под профиль стажера. Актуальные посты важнее." if mcp_context else ""}"""

        result = await self.generate(system_prompt, user_prompt)
        return result or "Не удалось сгенерировать контент. Попробуйте /learn ещё раз."

    async def generate_practice_intro(self, topic: dict, intern: dict, marathon_day: int = 1) -> str:
        """Генерирует вводный текст для практического задания"""
        system_prompt = f"""Ты — персональный наставник по системному мышлению.
{get_personalization_prompt(intern, marathon_day)}

Напиши краткое (3-5 предложений) введение к практическому заданию.
Объясни, зачем это задание и как оно связано с темой дня."""

        task = topic.get('task', '')
        work_product = topic.get('work_product', '')

        user_prompt = f"""Практическое задание: {topic.get('title')}
Основное понятие: {topic.get('main_concept')}

Задание: {task}
Рабочий продукт: {work_product}

Напиши краткое введение, которое мотивирует выполнить задание."""

        result = await self.generate(system_prompt, user_prompt)
        return result or ""

    async def generate_question(self, topic: dict, intern: dict, marathon_day: int = 1, bloom_level: int = None) -> str:
        """Генерирует вопрос по теме с учётом уровня Блума, ротации контекстов и метаданных темы

        Использует шаблоны вопросов из метаданных темы (topics/*.yaml) если доступны.
        Учитывает:
        - Блум 1 (Знание): вопросы "в чём разница"
        - Блум 2 (Понимание): открытые вопросы
        - Блум 3 (Применение): анализ, примеры из жизни/работы
        - Ротация контекстов по дню марафона
        """
        level = bloom_level or intern.get('bloom_level', 1)
        bloom = BLOOM_LEVELS.get(level, BLOOM_LEVELS[1])
        occupation = intern.get('occupation', '') or 'работа'
        study_duration = intern.get('study_duration', 15)
        interests = intern.get('interests', [])

        # Выбираем контекст для вопроса по дню (ротация)
        question_contexts = [
            f'профессии ("{occupation}")',
            f'интереса/хобби' + (f' ("{interests[(marathon_day - 1) % len(interests)]}")' if interests else ''),
            'повседневной жизни',
            'отношений с людьми',
            'личного развития',
            'принятия решений',
        ]
        context_idx = (marathon_day - 1) % len(question_contexts)
        question_context = question_contexts[context_idx]

        # Пробуем загрузить метаданные темы
        topic_id = topic.get('id', '')
        metadata = load_topic_metadata(topic_id) if topic_id else None

        # Получаем настройки вопросов из метаданных
        question_config = {}
        question_templates = []
        if metadata:
            question_config = get_bloom_questions(metadata, level, study_duration)
            question_templates = question_config.get('question_templates', [])
            logger.info(f"Загружены шаблоны вопросов для {topic_id}: bloom_{level}, {study_duration}мин, {len(question_templates)} шаблонов")

        # Определяем тип вопроса по уровню Блума
        question_type_hints = {
            1: "Задай вопрос на РАЗЛИЧЕНИЕ понятий (\"В чём разница между...\", \"Чем отличается...\").",
            2: "Задай ОТКРЫТЫЙ вопрос на понимание (\"Почему...\", \"Как вы понимаете...\", \"Объясните связь...\").",
            3: "Задай вопрос на ПРИМЕНЕНИЕ и АНАЛИЗ (\"Приведите пример из жизни\", \"Проанализируйте ситуацию\", \"Как бы вы объяснили коллеге...\")."
        }
        question_type_hint = question_type_hints.get(level, question_type_hints[1])

        # Формируем подсказки по шаблонам
        templates_hint = ""
        if question_templates:
            templates_hint = f"\nПРИМЕРЫ ВОПРОСОВ (используй как образец стиля):\n- " + "\n- ".join(question_templates[:3])

        system_prompt = f"""Ты генерируешь ТОЛЬКО ОДИН КОРОТКИЙ ВОПРОС. Ничего больше.

СТРОГО ЗАПРЕЩЕНО:
- Писать введение, объяснения, контекст или любой текст перед вопросом
- Писать заголовки типа "Вопрос:", "Вопрос для размышления:" и т.п.
- Писать примеры, истории, мотивацию
- Писать что-либо после вопроса

Выдай ТОЛЬКО сам вопрос — 1-3 предложения максимум.

КОНТЕКСТ ВОПРОСА (День {marathon_day}): {question_context}
Уровень сложности: {bloom['name']} — {bloom['desc']}
{question_type_hint}
{templates_hint}"""

        user_prompt = f"""Тема: {topic.get('title')}
Понятие: {topic.get('main_concept')}
Контекст: {question_context}

Выдай ТОЛЬКО вопрос (1-3 предложения), без введения и пояснений."""

        result = await self.generate(system_prompt, user_prompt)
        return result or bloom['question_type'].format(concept=topic.get('main_concept', 'эту тему'))

claude = ClaudeClient()

# ============= MCP CLIENT =============

class MCPClient:
    """Универсальный клиент для работы с MCP серверами Aisystant"""

    def __init__(self, url: str, name: str = "MCP", search_tool: str = "semantic_search"):
        self.base_url = url
        self.name = name
        self.search_tool = search_tool  # "semantic_search" для guides, "search" для knowledge
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _call(self, tool_name: str, arguments: dict) -> Optional[dict]:
        """Вызов инструмента MCP через JSON-RPC"""
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            },
            "id": self._next_id()
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.base_url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "result" in data:
                            return data["result"]
                        if "error" in data:
                            logger.error(f"{self.name} error: {data['error']}")
                            return None
                    else:
                        error = await resp.text()
                        logger.error(f"{self.name} HTTP error {resp.status}: {error}")
                        return None
        except asyncio.TimeoutError:
            logger.error(f"{self.name} request timeout")
            return None
        except Exception as e:
            logger.error(f"{self.name} exception: {e}")
            return None

    async def get_guides_list(self, lang: str = "ru", category: str = None) -> List[dict]:
        """Получить список всех руководств"""
        args = {"lang": lang}
        if category:
            args["category"] = category

        result = await self._call("get_guides_list", args)
        if result and "content" in result:
            # Парсим JSON из content
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        return json.loads(item.get("text", "[]"))
                    except json.JSONDecodeError:
                        pass
        return []

    async def get_guide_sections(self, guide_slug: str, lang: str = "ru") -> List[dict]:
        """Получить разделы конкретного руководства"""
        result = await self._call("get_guide_sections", {
            "guide_slug": guide_slug,
            "lang": lang
        })
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        return json.loads(item.get("text", "[]"))
                    except json.JSONDecodeError:
                        pass
        return []

    async def get_section_content(self, guide_slug: str, section_slug: str, lang: str = "ru") -> str:
        """Получить содержимое раздела"""
        result = await self._call("get_section_content", {
            "guide_slug": guide_slug,
            "section_slug": section_slug,
            "lang": lang
        })
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    return item.get("text", "")
        return ""

    async def semantic_search(self, query: str, lang: str = "ru", limit: int = 5, sort_by: str = None) -> List[dict]:
        """Семантический поиск по руководствам или базе знаний

        Args:
            query: поисковый запрос
            lang: язык (ru/en) — только для MCP-Guides
            limit: максимальное количество результатов
            sort_by: сортировка (например, "created_at:desc" для свежих постов)
        """
        args = {
            "query": query,
            "limit": limit
        }
        # Параметр lang только для semantic_search (MCP-Guides)
        if self.search_tool == "semantic_search":
            args["lang"] = lang
        if sort_by:
            args["sort"] = sort_by

        result = await self._call(self.search_tool, args)
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        data = json.loads(item.get("text", "[]"))
                        # Если sort_by указан и данные содержат дату, сортируем на клиенте
                        if sort_by and "desc" in sort_by and isinstance(data, list):
                            data.sort(key=lambda x: x.get('created_at', x.get('date', '')), reverse=True)
                        return data
                    except json.JSONDecodeError:
                        # Если не JSON, возвращаем как текст
                        return [{"text": item.get("text", "")}]
        return []

    async def search(self, query: str, limit: int = 5) -> List[dict]:
        """Поиск по базе знаний (knowledge MCP)

        Args:
            query: поисковый запрос
            limit: максимальное количество результатов
        """
        args = {
            "query": query,
            "limit": limit
        }

        result = await self._call("search", args)
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        data = json.loads(item.get("text", "[]"))
                        return data if isinstance(data, list) else [data]
                    except json.JSONDecodeError:
                        # Если не JSON, возвращаем как текст
                        return [{"text": item.get("text", "")}]
        return []

# Создаём клиенты для двух MCP серверов
mcp_guides = MCPClient(MCP_URL, "MCP-Guides")
mcp_knowledge = MCPClient(KNOWLEDGE_MCP_URL, "MCP-Knowledge", search_tool="search")

# Для обратной совместимости
mcp = mcp_guides

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

    # Собираем темы по неделям
    for i, topic in enumerate(TOPICS):
        week_id = 'week-1' if topic['day'] <= 7 else 'week-2'
        weeks[week_id]['total'] += 1
        if i in completed_topics:
            weeks[week_id]['completed'] += 1

    return [weeks['week-1'], weeks['week-2']]

def get_days_progress(completed_topics: list, marathon_day: int) -> list:
    """Получить прогресс по дням марафона"""
    days = []
    completed_set = set(completed_topics)

    for day in range(1, MARATHON_DAYS + 1):
        day_topics = [(i, t) for i, t in enumerate(TOPICS) if t['day'] == day]
        completed_count = sum(1 for i, _ in day_topics if i in completed_set)

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

# ============= КЛАВИАТУРЫ =============

def kb_experience() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['emoji']} {v['name']}", callback_data=f"exp_{k}")]
        for k, v in EXPERIENCE_LEVELS.items()
    ])

def kb_difficulty() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['emoji']} {v['name']}", callback_data=f"diff_{k}")]
        for k, v in DIFFICULTY_LEVELS.items()
    ])

def kb_learning_style() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['emoji']} {v['name']}", callback_data=f"style_{k}")]
        for k, v in LEARNING_STYLES.items()
    ])

def kb_study_duration() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['emoji']} {v['name']}", callback_data=f"duration_{k}")]
        for k, v in STUDY_DURATIONS.items()
    ])

def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Всё верно", callback_data="confirm"),
            InlineKeyboardButton(text="🔄 Заново", callback_data="restart")
        ]
    ])

def kb_learn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать сейчас", callback_data="learn")],
        [InlineKeyboardButton(text="⏰ Начать в установленное время", callback_data="later")]
    ])

def kb_update_profile() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Имя", callback_data="upd_name"),
         InlineKeyboardButton(text="💼 Занятие", callback_data="upd_occupation")],
        [InlineKeyboardButton(text="🎨 Интересы", callback_data="upd_interests")],
        [InlineKeyboardButton(text="💫 Что важно в жизни", callback_data="upd_motivation")],
        [InlineKeyboardButton(text="🎯 Что хочу изменить", callback_data="upd_goals")],
        [InlineKeyboardButton(text="⏱ Время на тему", callback_data="upd_duration"),
         InlineKeyboardButton(text="⏰ Расписание", callback_data="upd_schedule")],
        [InlineKeyboardButton(text="🎚 Сложность", callback_data="upd_bloom")],
        [InlineKeyboardButton(text="🎯 Выбор режима", callback_data="upd_mode")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="upd_cancel")]
    ])

def kb_bloom_level() -> InlineKeyboardMarkup:
    """Клавиатура для выбора уровня сложности"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{v['emoji']} {v['short_name']} «{v['name']}»",
            callback_data=f"bloom_{k}"
        )]
        for k, v in BLOOM_LEVELS.items()
    ])

def kb_bonus_question() -> InlineKeyboardMarkup:
    """Клавиатура для предложения дополнительного вопроса"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Да, давай сложнее!", callback_data="bonus_yes")],
        [InlineKeyboardButton(text="✅ Достаточно", callback_data="bonus_no")]
    ])

def kb_skip_topic() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой пропуска темы"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить тему", callback_data="skip_topic")]
    ])

def kb_marathon_start() -> InlineKeyboardMarkup:
    """Клавиатура для выбора даты старта марафона"""
    today = moscow_today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Сегодня", callback_data="start_today")],
        [InlineKeyboardButton(text=f"📅 Завтра ({tomorrow.strftime('%d.%m')})", callback_data="start_tomorrow")],
        [InlineKeyboardButton(text=f"📅 Послезавтра ({day_after.strftime('%d.%m')})", callback_data="start_day_after")]
    ])

def kb_submit_work_product() -> InlineKeyboardMarkup:
    """Клавиатура для практического задания"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить практику", callback_data="skip_practice")]
    ])

def progress_bar(completed: int, total: int) -> str:
    pct = int((completed / total) * 100) if total > 0 else 0
    return f"{'█' * (pct // 10)}{'░' * (10 - pct // 10)} {pct}%"

# ============= РОУТЕР =============

router = Router()

# --- Онбординг ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    
    if intern['onboarding_completed']:
        await message.answer(
            f"👋 С возвращением, {intern['name']}!\n\n"
            f"/learn — продолжить обучение\n"
            f"/progress — статистика\n"
            f"/profile — ваш профиль"
        )
        return

    await message.answer(
        "👋 Hello! I'm your AI guide for systemic self-development (AI System Track).\n"
        "I'll ask a few questions to personalize the content for you (~2 min).\n"
        "What is your name?\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👋 Здравствуйте! Я — ваш AI-помощник по системному развитию (AI System Track).\n"
        "Задам несколько вопросов, чтобы адаптировать материал под вас (~2 мин).\n"
        "Как вас зовут?"
    )
    await state.set_state(OnboardingStates.waiting_for_name)

@router.message(OnboardingStates.waiting_for_name)
async def on_name(message: Message, state: FSMContext):
    await update_intern(message.chat.id, name=message.text.strip())
    await message.answer(
        f"Приятно познакомиться, {message.text.strip()}!\n\n"
        "Чем вы занимаетесь?\n\n"
        "_Например: разработчик в IT-компании, студент экономфака, маркетолог в стартапе_",
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_occupation)

@router.message(OnboardingStates.waiting_for_occupation)
async def on_occupation(message: Message, state: FSMContext):
    await update_intern(message.chat.id, occupation=message.text.strip())
    await message.answer(
        "Расскажите о своих интересах и хобби.\n\n"
        "_Например: технологии, космос, кулинария, спорт, музыка, путешествия_\n\n"
        "_Это поможет приводить близкие вам примеры._",
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_interests)

@router.message(OnboardingStates.waiting_for_interests)
async def on_interests(message: Message, state: FSMContext):
    interests = [i.strip() for i in message.text.replace(',', ';').split(';') if i.strip()]
    await update_intern(message.chat.id, interests=interests)
    await message.answer(
        "*Что для вас по-настоящему важно в жизни?*\n\n"
        "_Это поможет мне добавлять мотивационные блоки, которые вас зацепят._",
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_motivation)

@router.message(OnboardingStates.waiting_for_motivation)
async def on_motivation(message: Message, state: FSMContext):
    await update_intern(message.chat.id, motivation=message.text.strip())
    await message.answer(
        "*Что хотите изменить* в своей жизни или работе?\n\n"
        "_Это определит, как я буду персонализировать материалы под вас._",
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_goals)

@router.message(OnboardingStates.waiting_for_goals)
async def on_goals(message: Message, state: FSMContext):
    await update_intern(message.chat.id, goals=message.text.strip())
    await message.answer(
        "Сколько минут готовы уделять изучению одной темы?\n\n"
        "_Совет: лучше начать с малого и постепенно увеличивать. "
        "5-10 минут каждый день эффективнее, чем 25 минут раз в неделю._",
        parse_mode="Markdown",
        reply_markup=kb_study_duration()
    )
    await state.set_state(OnboardingStates.waiting_for_study_duration)

@router.callback_query(OnboardingStates.waiting_for_study_duration, F.data.startswith("duration_"))
async def on_duration(callback: CallbackQuery, state: FSMContext):
    duration = int(callback.data.replace("duration_", ""))
    await update_intern(callback.message.chat.id, study_duration=duration)
    await callback.answer()
    await callback.message.edit_text(
        "Во сколько напоминать о новой теме?\n\n"
        "_Напишите время в формате ЧЧ:ММ (например: 09:00)_\n"
        "_Часовой пояс: UTC+3 (Москва)_",
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_schedule)

@router.message(OnboardingStates.waiting_for_schedule)
async def on_schedule(message: Message, state: FSMContext):
    try:
        h, m = map(int, message.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except:
        await message.answer("Формат: ЧЧ:ММ (например 09:00)")
        return

    # Нормализуем формат времени (с ведущими нулями)
    normalized_time = f"{h:02d}:{m:02d}"
    await update_intern(message.chat.id, schedule_time=normalized_time)

    await message.answer(
        "🗓 *Когда начнём марафон?*\n\n"
        "Марафон длится *14 дней*. Каждый день — 2 темы:\n"
        "• *Теория* — материал + вопрос для размышления\n"
        "• *Практика* — задание + рабочий продукт\n\n"
        "Выберите дату старта:",
        parse_mode="Markdown",
        reply_markup=kb_marathon_start()
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

    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    interests_str = ', '.join(intern['interests']) if intern['interests'] else 'не указаны'
    motivation_short = intern['motivation'][:100] + '...' if len(intern['motivation']) > 100 else intern['motivation']
    goals_short = intern['goals'][:100] + '...' if len(intern['goals']) > 100 else intern['goals']

    await callback.message.edit_text(
        f"📋 *Ваш профиль:*\n\n"
        f"👤 *Имя:* {intern['name']}\n"
        f"💼 *Занятие:* {intern['occupation']}\n"
        f"🎨 *Интересы:* {interests_str}\n\n"
        f"💫 *Что важно:* {motivation_short}\n"
        f"🎯 *Что изменить:* {goals_short}\n\n"
        f"{duration.get('emoji', '')} {duration.get('name', '')} на тему\n"
        f"⏰ Напоминание в {intern['schedule_time']}\n"
        f"🗓 Старт марафона: *{start_date.strftime('%d.%m.%Y')}*\n\n"
        f"Всё верно?",
        parse_mode="Markdown",
        reply_markup=kb_confirm()
    )
    await state.set_state(OnboardingStates.confirming_profile)

@router.callback_query(OnboardingStates.confirming_profile, F.data == "confirm")
async def on_confirm(callback: CallbackQuery, state: FSMContext):
    await update_intern(callback.message.chat.id, onboarding_completed=True)
    intern = await get_intern(callback.message.chat.id)
    marathon_day = get_marathon_day(intern)
    start_date = intern.get('marathon_start_date')

    await callback.answer("Сохранено!")

    # Определяем, когда старт
    if start_date:
        today = moscow_today()
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        if start_date > today:
            start_msg = f"🗓 Марафон начнётся *{start_date.strftime('%d.%m.%Y')}*"
            can_start_now = False
        else:
            start_msg = f"🗓 *День {marathon_day} из {MARATHON_DAYS}*"
            can_start_now = True
    else:
        start_msg = "🗓 Дата старта не задана"
        can_start_now = False

    # Приветственное сообщение для марафона (English + Russian)
    await callback.message.edit_text(
        f"🎉 *Welcome to the Marathon, {intern['name']}!*\n\n"
        f"14 days from casual learner to systematic practitioner.\n"
        f"📅 {MARATHON_DAYS} days — 2 topics per day (theory + practice)\n"
        f"⏱ {intern['study_duration']} minutes per topic\n"
        f"⏰ Daily reminders at {intern['schedule_time']}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"🎉 *Добро пожаловать в марафон, {intern['name']}!*\n\n"
        f"14 дней от случайного ученика к систематическому.\n"
        f"📅 {MARATHON_DAYS} дней — по 2 темы в день (теория + практика)\n"
        f"⏱ {intern['study_duration']} минут на каждую тему\n"
        f"⏰ Напоминания каждый день в {intern['schedule_time']}\n\n"
        f"{start_msg}",
        parse_mode="Markdown",
        reply_markup=kb_learn()
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
        await message.answer("Сначала /start")
        return
    await send_topic(message.chat.id, state, message.bot)

@router.callback_query(F.data == "learn")
async def cb_learn(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup()
    await send_topic(callback.message.chat.id, state, callback.bot)

@router.callback_query(F.data == "later")
async def cb_later(callback: CallbackQuery):
    intern = await get_intern(callback.message.chat.id)
    await callback.answer()
    await callback.message.edit_text(f"Жду вас в {intern['schedule_time']}! Или /learn")

@router.message(Command("progress"))
async def cmd_progress(message: Message):
    intern = await get_intern(message.chat.id)
    if not intern['onboarding_completed']:
        await message.answer("Сначала /start")
        return

    done = len(intern['completed_topics'])
    total = get_total_topics()
    marathon_day = get_marathon_day(intern)
    days_progress = get_days_progress(intern['completed_topics'], marathon_day)

    # Формируем прогресс по дням (показываем первые 7 или 14 в зависимости от текущего дня)
    days_text = ""
    for d in days_progress:
        day_num = d['day']
        if day_num > marathon_day + 1:
            break  # Не показываем далёкие дни

        if d['status'] == 'completed':
            emoji = "✅"
        elif d['status'] == 'in_progress':
            emoji = "🔄"
        elif d['status'] == 'available':
            emoji = "📍"
        else:
            emoji = "🔒"

        days_text += f"{emoji} День {day_num}: {d['completed']}/{d['total']}\n"

    # Неделя 1 / Неделя 2
    weeks = get_sections_progress(intern['completed_topics'])
    weeks_text = ""
    for i, week in enumerate(weeks):
        pct = int((week['completed'] / week['total']) * 100) if week['total'] > 0 else 0
        bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
        status = " ✅" if week['completed'] == week['total'] else ""
        weeks_text += f"{'1️⃣' if i == 0 else '2️⃣'} Неделя {i + 1}: {bar} {week['completed']}/{week['total']}{status}\n"

    await message.answer(
        f"📊 *Прогресс: {intern['name']}*\n\n"
        f"🗓 *День {marathon_day} из {MARATHON_DAYS}*\n"
        f"✅ {done} из {total} тем\n"
        f"{progress_bar(done, total)}\n\n"
        f"*По неделям*\n"
        f"{weeks_text}\n"
        f"*По дням*\n"
        f"{days_text}\n"
        f"/learn — продолжить обучение",
        parse_mode="Markdown"
    )

@router.message(Command("profile"))
async def cmd_profile(message: Message):
    intern = await get_intern(message.chat.id)
    if not intern['onboarding_completed']:
        await message.answer("Сначала /start")
        return

    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    interests_str = ', '.join(intern['interests']) if intern['interests'] else 'не указаны'
    motivation_short = intern['motivation'][:100] + '...' if len(intern.get('motivation', '')) > 100 else intern.get('motivation', '')
    goals_short = intern['goals'][:100] + '...' if len(intern['goals']) > 100 else intern['goals']

    await message.answer(
        f"👤 *{intern['name']}*\n"
        f"💼 {intern.get('occupation', '')}\n"
        f"🎨 {interests_str}\n\n"
        f"💫 *Что важно:* {motivation_short or 'не указано'}\n"
        f"🎯 *Что изменить:* {goals_short}\n\n"
        f"{duration.get('emoji', '')} {duration.get('name', '')} на тему\n"
        f"{bloom['emoji']} Уровень: {bloom['short_name']} «{bloom['name']}»\n"
        f"⏰ Напоминание в {intern['schedule_time']}\n\n"
        f"🆔 `{message.chat.id}`\n\n"
        f"/update — обновить профиль",
        parse_mode="Markdown"
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 *Основные команды:*\n\n"
        "/learn — получить новую тему для изучения\n"
        "/mode — выбор режима (Марафон/Лента)\n"
        "/progress — посмотреть свой прогресс\n"
        "/profile — посмотреть свой профиль\n"
        "/update — обновить профиль\n\n"
        "*Как работает обучение:*\n"
        "1. Я отправляю персонализированный материал\n"
        "2. Вы изучаете его (5-25 мин)\n"
        "3. Отвечаете на вопрос для закрепления\n"
        "4. Тема засчитывается в прогресс\n\n"
        "Материал буду отправлять в заданное время или по /learn\n\n"
        "🔗 [Мастерская инженеров-менеджеров](https://system-school.ru/)\n\n"
        "💬 Замечания и предложения: @tserentserenov",
        parse_mode="Markdown"
    )

# --- Обновление профиля ---

@router.message(Command("update"))
async def cmd_update(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    if not intern['onboarding_completed']:
        await message.answer("Сначала пройдите онбординг: /start")
        return

    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    # Получаем дату старта марафона
    start_date = intern.get('marathon_start_date')
    if start_date:
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        marathon_start_str = start_date.strftime('%d.%m.%Y')
    else:
        marathon_start_str = "не задана"

    marathon_day = get_marathon_day(intern)

    interests_str = ', '.join(intern['interests']) if intern['interests'] else 'не указаны'
    motivation_short = intern.get('motivation', '')[:80] + '...' if len(intern.get('motivation', '')) > 80 else intern.get('motivation', '') or 'не указано'
    goals_short = intern['goals'][:80] + '...' if len(intern['goals']) > 80 else intern['goals'] or 'не указано'

    await message.answer(
        f"👤 *{intern['name']}*\n"
        f"💼 {intern.get('occupation', '') or 'не указано'}\n"
        f"🎨 {interests_str}\n\n"
        f"💫 *Важно:* {motivation_short}\n"
        f"🎯 *Изменить:* {goals_short}\n\n"
        f"{duration.get('emoji', '')} {duration.get('name', '')} на тему\n"
        f"{bloom['emoji']} Уровень: {bloom['short_name']}\n"
        f"🗓 Старт марафона: {marathon_start_str} (день {marathon_day})\n"
        f"⏰ Напоминание в {intern['schedule_time']}\n\n"
        f"*Что хотите обновить?*",
        parse_mode="Markdown",
        reply_markup=kb_update_profile()
    )
    await state.set_state(UpdateStates.choosing_field)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_name")
async def on_upd_name(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    await callback.answer()
    await callback.message.edit_text(
        f"👤 *Ваше имя:* {intern['name']}\n\n"
        "Как вас зовут?",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_name)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_occupation")
async def on_upd_occupation(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    await callback.answer()
    await callback.message.edit_text(
        f"💼 *Ваше занятие:* {intern.get('occupation', '') or 'не указано'}\n\n"
        "Чем вы занимаетесь?",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_occupation)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_interests")
async def on_upd_interests(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    interests_str = ', '.join(intern['interests']) if intern['interests'] else 'не указаны'
    await callback.answer()
    await callback.message.edit_text(
        f"🎨 *Ваши интересы:* {interests_str}\n\n"
        "_Например: технологии, космос, кулинария, спорт, музыка, путешествия_\n\n"
        "Расскажите о своих интересах и хобби:",
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
    await callback.answer()
    await callback.message.edit_text(
        f"🎯 *Что хотите изменить:*\n{intern['goals'] or 'не указано'}\n\n"
        "Что хотите изменить в своей жизни или работе?",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_goals)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_duration")
async def on_upd_duration(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    await callback.answer()
    await callback.message.edit_text(
        f"⏱ *Текущее время:* {duration.get('emoji', '')} {duration.get('name', '')}\n\n"
        "Сколько минут готовы уделять изучению одной темы?",
        parse_mode="Markdown",
        reply_markup=kb_study_duration()
    )
    await state.set_state(UpdateStates.updating_duration)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_schedule")
async def on_upd_schedule(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    await callback.answer()
    await callback.message.edit_text(
        f"⏰ *Текущее время напоминания:* {intern['schedule_time']}\n\n"
        "Во сколько напоминать о новой теме?\n"
        "_Напишите время в формате ЧЧ:ММ (например: 09:00)_\n"
        "_Часовой пояс: UTC+3 (Москва)_",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_schedule)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_bloom")
async def on_upd_bloom(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])
    await callback.answer()
    await callback.message.edit_text(
        f"🎚 *Текущий уровень сложности:* {bloom['emoji']} {bloom['short_name']} «{bloom['name']}»\n"
        f"_{bloom['desc']}_\n\n"
        f"Пройдено тем на этом уровне: {intern['topics_at_current_bloom']}/{BLOOM_AUTO_UPGRADE_AFTER}\n\n"
        "Выберите новый уровень сложности вопросов:",
        parse_mode="Markdown",
        reply_markup=kb_bloom_level()
    )
    await state.set_state(UpdateStates.updating_bloom_level)

@router.callback_query(UpdateStates.updating_bloom_level, F.data.startswith("bloom_"))
async def on_save_bloom(callback: CallbackQuery, state: FSMContext):
    level = int(callback.data.replace("bloom_", ""))
    await update_intern(callback.message.chat.id, bloom_level=level, topics_at_current_bloom=0)

    bloom = BLOOM_LEVELS.get(level, BLOOM_LEVELS[1])
    await callback.answer(f"Уровень: {bloom['short_name']}")
    await callback.message.edit_text(
        f"✅ Уровень сложности изменён на *{bloom['short_name']} «{bloom['name']}»*!\n\n"
        f"{bloom['desc']}\n\n"
        f"/learn — продолжить обучение\n"
        f"/update — обновить ещё что-то",
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
    today = moscow_today()

    if callback.data == "start_today":
        start_date = today
        date_text = "сегодня"
    elif callback.data == "start_tomorrow":
        start_date = today + timedelta(days=1)
        date_text = "завтра"
    else:  # start_day_after
        start_date = today + timedelta(days=2)
        date_text = "послезавтра"

    await update_intern(callback.message.chat.id, marathon_start_date=start_date)

    await callback.answer("Дата старта обновлена!")
    await callback.message.edit_text(
        f"✅ Дата старта марафона изменена!\n\n"
        f"Новая дата: *{start_date.strftime('%d.%m.%Y')}* ({date_text})\n\n"
        f"/learn — продолжить обучение\n"
        f"/update — обновить ещё что-то",
        parse_mode="Markdown"
    )
    await state.clear()

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_cancel")
async def on_upd_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Отменено")
    await callback.message.edit_text("Хорошо! Можете продолжить обучение: /learn")
    await state.clear()

@router.message(UpdateStates.updating_motivation)
async def on_save_motivation(message: Message, state: FSMContext):
    await update_intern(message.chat.id, motivation=message.text.strip())
    await message.answer(
        "✅ Обновлено!\n\n"
        "Теперь мотивационные блоки будут ещё точнее.\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то"
    )
    await state.clear()

@router.message(UpdateStates.updating_goals)
async def on_save_goals(message: Message, state: FSMContext):
    await update_intern(message.chat.id, goals=message.text.strip())
    await message.answer(
        "✅ Обновлено!\n\n"
        "Теперь материалы будут персонализированы под ваши цели.\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то"
    )
    await state.clear()

@router.message(UpdateStates.updating_name)
async def on_save_name(message: Message, state: FSMContext):
    await update_intern(message.chat.id, name=message.text.strip())
    await message.answer(
        f"✅ Имя изменено на *{message.text.strip()}*!\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то",
        parse_mode="Markdown"
    )
    await state.clear()

@router.message(UpdateStates.updating_occupation)
async def on_save_occupation(message: Message, state: FSMContext):
    await update_intern(message.chat.id, occupation=message.text.strip())
    await message.answer(
        "✅ Занятие обновлено!\n\n"
        "Теперь примеры будут из вашей области.\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то"
    )
    await state.clear()

@router.message(UpdateStates.updating_interests)
async def on_save_interests(message: Message, state: FSMContext):
    interests = [i.strip() for i in message.text.replace(',', ';').split(';') if i.strip()]
    await update_intern(message.chat.id, interests=interests)
    await message.answer(
        "✅ Интересы обновлены!\n\n"
        "Теперь примеры будут ближе к вашим хобби.\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то"
    )
    await state.clear()

@router.callback_query(UpdateStates.updating_duration, F.data.startswith("duration_"))
async def on_save_duration(callback: CallbackQuery, state: FSMContext):
    duration = int(callback.data.replace("duration_", ""))
    await update_intern(callback.message.chat.id, study_duration=duration)
    duration_info = STUDY_DURATIONS.get(str(duration), {})
    await callback.answer("Сохранено!")
    await callback.message.edit_text(
        f"✅ Время на тему изменено: {duration_info.get('emoji', '')} *{duration_info.get('name', '')}*\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то",
        parse_mode="Markdown"
    )
    await state.clear()

@router.message(UpdateStates.updating_schedule)
async def on_save_schedule(message: Message, state: FSMContext):
    try:
        h, m = map(int, message.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except:
        await message.answer("Формат: ЧЧ:ММ (например 09:00)")
        return

    # Нормализуем формат времени (с ведущими нулями)
    normalized_time = f"{h:02d}:{m:02d}"
    await update_intern(message.chat.id, schedule_time=normalized_time)
    await message.answer(
        f"✅ Время напоминания изменено на *{normalized_time}*!\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то",
        parse_mode="Markdown"
    )
    await state.clear()

@router.message(LearningStates.waiting_for_answer)
async def on_answer(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)

    if len(message.text.strip()) < 20:
        await message.answer("Напишите подробнее (хотя бы 2-3 предложения)")
        return

    # Сохраняем ответ
    await save_answer(message.chat.id, intern['current_topic_index'], message.text.strip())

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
    bloom = BLOOM_LEVELS.get(bloom_level, BLOOM_LEVELS[1])

    # Сообщение о повышении уровня
    upgrade_msg = ""
    if level_upgraded:
        upgrade_msg = f"\n\n🎉 *Поздравляем!* Вы перешли на *{bloom['short_name']} «{bloom['name']}»*!"

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
    if next_available:
        next_topic = next_available[0][1]  # (index, topic) -> topic
        next_topic_hint = f"\n\n📚 *Следующая тема:* {next_topic['title']}"

    # Если уровень ниже максимального — предлагаем дополнительный вопрос
    if intern['bloom_level'] < 3:
        # Сохраняем индекс темы в state для бонусного вопроса
        await state.update_data(topic_index=intern['current_topic_index'])

        await message.answer(
            f"✅ *Тема засчитана!*\n\n"
            f"{progress_bar(done, total)}\n"
            f"{bloom['short_name']}{upgrade_msg}{next_topic_hint}\n\n"
            f"Хотите дополнительный вопрос посложнее?",
            parse_mode="Markdown",
            reply_markup=kb_bonus_question()
        )
        # Не очищаем state — ждём выбора
    else:
        await message.answer(
            f"✅ *Тема засчитана!*\n\n"
            f"{progress_bar(done, total)}\n"
            f"{bloom['short_name']}{upgrade_msg}{next_topic_hint}\n\n"
            f"/learn — следующая тема",
            parse_mode="Markdown"
        )
        await state.clear()

@router.callback_query(F.data == "bonus_yes")
async def on_bonus_yes(callback: CallbackQuery, state: FSMContext):
    """Пользователь хочет дополнительный вопрос посложнее"""
    await callback.answer()

    data = await state.get_data()
    topic_index = data.get('topic_index', 0)

    intern = await get_intern(callback.message.chat.id)
    topic = get_topic(topic_index)

    if not topic:
        await callback.message.edit_text("Не удалось найти тему. /learn для продолжения")
        await state.clear()
        return

    await callback.message.edit_text("⏳ Генерирую вопрос посложнее...")

    # Генерируем вопрос следующего уровня
    marathon_day = get_marathon_day(intern)
    next_level = min(intern['bloom_level'] + 1, 3)
    question = await claude.generate_question(topic, intern, marathon_day=marathon_day, bloom_level=next_level)

    bloom = BLOOM_LEVELS.get(next_level, BLOOM_LEVELS[1])

    await callback.message.answer(
        f"🚀 *Бонусный вопрос* ({bloom['short_name']})\n\n"
        f"{question}\n\n"
        f"Напишите ответ 👇",
        parse_mode="Markdown"
    )
    await state.set_state(LearningStates.waiting_for_bonus_answer)

@router.callback_query(F.data == "bonus_no")
async def on_bonus_no(callback: CallbackQuery, state: FSMContext):
    """Пользователь отказался от дополнительного вопроса"""
    await callback.answer("Хорошо!")
    await callback.message.edit_text(
        callback.message.text + "\n\n/learn — следующая тема",
        parse_mode="Markdown"
    )
    await state.clear()

@router.message(LearningStates.waiting_for_bonus_answer)
async def on_bonus_answer(message: Message, state: FSMContext):
    """Обработка ответа на бонусный вопрос"""
    if len(message.text.strip()) < 20:
        await message.answer("Напишите подробнее (хотя бы 2-3 предложения)")
        return

    intern = await get_intern(message.chat.id)
    data = await state.get_data()
    topic_index = data.get('topic_index', 0)

    # Сохраняем ответ на бонусный вопрос
    await save_answer(message.chat.id, topic_index, f"[BONUS] {message.text.strip()}")

    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    # Получаем информацию о следующей доступной теме
    next_available = get_available_topics(intern)
    next_topic_hint = ""
    if next_available:
        next_topic = next_available[0][1]  # (index, topic) -> topic
        next_topic_hint = f"\n\n📚 *Следующая тема:* {next_topic['title']}"

    await message.answer(
        f"🌟 *Отлично!* Бонусный вопрос засчитан!\n\n"
        f"Вы тренируете навыки *{bloom['short_name']}* и выше.{next_topic_hint}\n\n"
        f"/learn — следующая тема",
        parse_mode="Markdown"
    )
    await state.clear()

@router.callback_query(LearningStates.waiting_for_answer, F.data == "skip_topic")
async def on_skip_topic(callback: CallbackQuery, state: FSMContext):
    """Пропуск теоретической темы без ответа"""
    intern = await get_intern(callback.message.chat.id)

    next_index = intern['current_topic_index'] + 1
    await update_intern(callback.message.chat.id, current_topic_index=next_index)

    topic = get_topic(intern['current_topic_index'])
    topic_title = topic['title'] if topic else "тема"

    await callback.answer("Тема пропущена")
    await callback.message.edit_text(
        f"⏭ *Тема пропущена:* {topic_title}\n\n"
        f"_Пропущенные темы не засчитываются в прогресс._\n\n"
        f"/learn — следующая тема\n"
        f"/progress — посмотреть прогресс",
        parse_mode="Markdown"
    )
    await state.clear()


@router.message(LearningStates.waiting_for_work_product)
async def on_work_product(message: Message, state: FSMContext):
    """Обработка отправки рабочего продукта"""
    intern = await get_intern(message.chat.id)

    if len(message.text.strip()) < 3:
        await message.answer("Напишите хотя бы название рабочего продукта (например: «Список в заметках»)")
        return

    # Сохраняем ответ (рабочий продукт)
    await save_answer(message.chat.id, intern['current_topic_index'], f"[РП] {message.text.strip()}")

    # Обновляем прогресс
    completed = intern['completed_topics'] + [intern['current_topic_index']]

    # Обновляем счётчик тем за сегодня
    today = moscow_today()
    topics_today = get_topics_today(intern) + 1

    await update_intern(
        message.chat.id,
        completed_topics=completed,
        current_topic_index=intern['current_topic_index'] + 1,
        topics_today=topics_today,
        last_topic_date=today
    )

    done = len(completed)
    total = get_total_topics()
    marathon_day = get_marathon_day(intern)

    # Проверяем, завершён ли день
    day_topics = get_topics_for_day(marathon_day)
    day_completed = sum(1 for i, _ in enumerate(TOPICS) if TOPICS[i]['day'] == marathon_day and i in completed)

    if day_completed >= len(day_topics):
        # День полностью завершён
        await message.answer(
            f"🎉 *День {marathon_day} завершён!*\n\n"
            f"✅ Теория пройдена\n"
            f"✅ Практика выполнена\n"
            f"📝 РП: {message.text.strip()}\n\n"
            f"{progress_bar(done, total)}\n\n"
            f"Отличная работа! Возвращайтесь завтра за новыми темами.\n\n"
            f"/progress — посмотреть прогресс",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"✅ *Практика засчитана!*\n\n"
            f"📝 РП: {message.text.strip()}\n\n"
            f"{progress_bar(done, total)}\n\n"
            f"/learn — следующая тема",
            parse_mode="Markdown"
        )

    await state.clear()


@router.callback_query(LearningStates.waiting_for_work_product, F.data == "skip_practice")
async def on_skip_practice(callback: CallbackQuery, state: FSMContext):
    """Пропуск практической темы"""
    intern = await get_intern(callback.message.chat.id)

    next_index = intern['current_topic_index'] + 1
    await update_intern(callback.message.chat.id, current_topic_index=next_index)

    topic = get_topic(intern['current_topic_index'])
    topic_title = topic['title'] if topic else "практика"

    await callback.answer("Практика пропущена")
    await callback.message.edit_text(
        f"⏭ *Практика пропущена:* {topic_title}\n\n"
        f"_Пропущенные практики не засчитываются в прогресс._\n\n"
        f"/learn — следующая тема\n"
        f"/progress — посмотреть прогресс",
        parse_mode="Markdown"
    )
    await state.clear()

# --- Отправка темы ---

async def send_topic(chat_id: int, state: FSMContext, bot: Bot):
    intern = await get_intern(chat_id)
    marathon_day = get_marathon_day(intern)

    # Автоматический запуск марафона при первом /learn
    if marathon_day == 0:
        start_date = intern.get('marathon_start_date')
        if start_date:
            # Дата старта в будущем
            await bot.send_message(
                chat_id,
                f"🗓 Марафон ещё не начался.\n\n"
                f"Старт: *{start_date.strftime('%d.%m.%Y')}*\n\n"
                f"Если хотите изменить дату — /update",
                parse_mode="Markdown"
            )
            return
        else:
            # Автоматически запускаем марафон сегодня
            today = moscow_today()
            await update_intern(chat_id, marathon_start_date=today)
            await bot.send_message(
                chat_id,
                f"🚀 *Марафон запущен!*\n\n"
                f"Старт: *{today.strftime('%d.%m.%Y')}* (сегодня)\n\n"
                f"Если хотите изменить дату старта — /update\n\n"
                f"А сейчас — ваша первая тема! 👇",
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
            f"🎯 *Сегодня вы уже прошли {topics_today} тем — это максимум!*\n\n"
            f"Лимит: *{MAX_TOPICS_PER_DAY} тем в день* (можно нагнать 1 день)\n\n"
            f"Регулярность > Интенсивность\n\n"
            f"Возвращайтесь завтра! Или в *{intern['schedule_time']}* я сам напомню.",
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
                f"✅ *День {marathon_day} завершён!*\n\n"
                f"Пройдено тем: {completed_count}/{total_topics}\n\n"
                f"Следующие темы откроются завтра.\n"
                f"Возвращайтесь в *{intern['schedule_time']}*!",
                parse_mode="Markdown"
            )
            return

        if completed_count >= total_topics:
            # Марафон пройден — короткое сообщение (пользователь сам запросил /learn)
            weeks = get_sections_progress(intern['completed_topics'])
            weeks_text = ""
            for i, week in enumerate(weeks):
                pct = int((week['completed'] / week['total']) * 100) if week['total'] > 0 else 0
                bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
                weeks_text += f"{'1️⃣' if i == 0 else '2️⃣'} Неделя {i + 1}: {bar} {week['completed']}/{week['total']} ✅\n"

            await bot.send_message(
                chat_id,
                "🎉 *Поздравляем! Марафон пройден!*\n\n"
                f"Вы прошли все *{MARATHON_DAYS} дней* и *{total_topics} тем*.\n\n"
                f"📊 *Ваша статистика:*\n"
                f"{weeks_text}\n"
                "Заходите в [Мастерскую](https://system-school.ru/) для продвинутых программ.",
                parse_mode="Markdown"
            )
            return

        await bot.send_message(
            chat_id,
            "⚠️ Что-то пошло не так. Попробуйте /learn ещё раз.",
            parse_mode="Markdown"
        )
        return

    # Отправляем тему в зависимости от типа
    topic_type = topic.get('type', 'theory')

    if topic_type == 'theory':
        await send_theory_topic(chat_id, topic, intern, state, bot)
    else:
        await send_practice_topic(chat_id, topic, intern, state, bot)


async def send_theory_topic(chat_id: int, topic: dict, intern: dict, state: FSMContext, bot: Bot):
    """Отправка теоретической темы"""
    marathon_day = get_marathon_day(intern)
    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    # Показываем, что бот работает
    await bot.send_chat_action(chat_id=chat_id, action="typing")
    await bot.send_message(chat_id, "⏳ Генерирую персональный материал...")

    content = await claude.generate_content(topic, intern, marathon_day=marathon_day, mcp_client=mcp_guides, knowledge_client=mcp_knowledge)
    question = await claude.generate_question(topic, intern, marathon_day=marathon_day)

    header = (
        f"📚 *День {marathon_day} — Теория*\n"
        f"*{topic['title']}*\n"
        f"⏱ {intern['study_duration']} минут\n\n"
    )

    full = header + content
    if len(full) > 4000:
        await bot.send_message(chat_id, header, parse_mode="Markdown")
        for i in range(0, len(content), 4000):
            await bot.send_message(chat_id, content[i:i+4000])
    else:
        await bot.send_message(chat_id, full, parse_mode="Markdown")

    # Вопрос отдельным сообщением
    await bot.send_message(
        chat_id,
        f"💭 *Вопрос для размышления* ({bloom['short_name']})\n\n"
        f"{question}\n\n"
        f"_Напишите ответ в сообщении. Он не проверяется — "
        f"после получения любого ответа тема считается пройденной._",
        parse_mode="Markdown",
        reply_markup=kb_skip_topic()
    )

    await state.set_state(LearningStates.waiting_for_answer)


async def send_practice_topic(chat_id: int, topic: dict, intern: dict, state: FSMContext, bot: Bot):
    """Отправка практической темы"""
    marathon_day = get_marathon_day(intern)

    # Показываем, что бот работает
    await bot.send_chat_action(chat_id=chat_id, action="typing")
    await bot.send_message(chat_id, "⏳ Готовлю практическое задание...")

    # Генерируем краткое введение
    intro = await claude.generate_practice_intro(topic, intern, marathon_day=marathon_day)

    task = topic.get('task', '')
    work_product = topic.get('work_product', '')
    examples = topic.get('work_product_examples', [])

    examples_text = ""
    if examples:
        examples_text = "\n*Примеры РП:*\n" + "\n".join([f"• {ex}" for ex in examples])

    header = (
        f"✏️ *День {marathon_day} — Практика*\n"
        f"*{topic['title']}*\n\n"
    )

    content = f"{intro}\n\n" if intro else ""
    content += f"📋 *Задание:*\n{task}\n\n"
    content += f"🎯 *Рабочий продукт:* {work_product}"
    content += examples_text

    full = header + content
    if len(full) > 4000:
        await bot.send_message(chat_id, header, parse_mode="Markdown")
        await bot.send_message(chat_id, content, parse_mode="Markdown")
    else:
        await bot.send_message(chat_id, full, parse_mode="Markdown")

    # Запрос рабочего продукта
    await bot.send_message(
        chat_id,
        "📝 *Когда выполните задание:*\n\n"
        "Напишите название своего рабочего продукта.\n\n"
        f"_Например: «{examples[0] if examples else work_product}»_\n\n"
        "_Проверки нет — просто напишите, что сделали, и практика засчитается._",
        parse_mode="Markdown",
        reply_markup=kb_submit_work_product()
    )

    await state.set_state(LearningStates.waiting_for_work_product)

# ============= ПЛАНИРОВЩИК =============

scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)

# Глобальный dispatcher для доступа к FSM storage
_dispatcher: Optional[Dispatcher] = None

async def send_scheduled_topic(chat_id: int, bot: Bot):
    """Отправка темы по расписанию"""
    intern = await get_intern(chat_id)
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
            weeks = get_sections_progress(intern['completed_topics'])
            weeks_text = ""
            for i, week in enumerate(weeks):
                pct = int((week['completed'] / week['total']) * 100) if week['total'] > 0 else 0
                bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
                weeks_text += f"{'1️⃣' if i == 0 else '2️⃣'} Неделя {i + 1}: {bar} {week['completed']}/{week['total']} ✅\n"

            await bot.send_message(
                chat_id,
                "🎉 *Поздравляем! Марафон пройден!*\n\n"
                f"Вы прошли все *{MARATHON_DAYS} дней* и *{total} тем*.\n\n"
                f"📊 *Ваша статистика:*\n"
                f"{weeks_text}\n"
                "Теперь вы — *Практикующий ученик* с базовыми практиками:\n"
                "• Слоты саморазвития\n"
                "• Трекер практик\n"
                "• Мимолётные заметки\n"
                "• Рабочие продукты\n\n"
                "Хотите продолжить развитие?\n"
                "Заходите в [Мастерскую инженеров-менеджеров](https://system-school.ru/)!",
                parse_mode="Markdown"
            )
        return

    if topic_index is not None and topic_index != intern['current_topic_index']:
        await update_intern(chat_id, current_topic_index=topic_index)

    # Планируем напоминания (+1ч и +3ч)
    await schedule_reminders(chat_id, intern)

    # Отправляем тему
    topic_type = topic.get('type', 'theory')

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
    async with db_pool.acquire() as conn:
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
            f"⏰ *Напоминание*\n\n"
            f"День {marathon_day} марафона ждёт вас!\n\n"
            f"Всего 2 темы на сегодня: теория и практика.\n\n"
            f"/learn — начать",
            parse_mode="Markdown"
        )
    elif reminder_type == '+3h':
        await bot.send_message(
            chat_id,
            f"🔔 *Последнее напоминание*\n\n"
            f"День {marathon_day} ещё не начат.\n\n"
            f"Помните: *регулярность > интенсивность*.\n"
            f"Даже 15 минут сегодня — это прогресс.\n\n"
            f"/learn — начать",
            parse_mode="Markdown"
        )


async def check_reminders():
    """Проверяет и отправляет запланированные напоминания"""
    now = moscow_now()
    # Убираем timezone для совместимости с TIMESTAMP (без timezone)
    now_naive = now.replace(tzinfo=None)

    async with db_pool.acquire() as conn:
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
        for chat_id in chat_ids:
            try:
                await send_scheduled_topic(chat_id, bot)
                logger.info(f"[Scheduler] Отправлена тема пользователю {chat_id}")
            except Exception as e:
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
    await callback.answer(
        "Кнопка устарела. Используйте /learn для продолжения.",
        show_alert=True
    )

@router.message()
async def on_unknown_message(message: Message, state: FSMContext):
    """Обработка сообщений вне FSM-состояний"""
    current_state = await state.get_state()

    # Если пользователь в каком-то состоянии — логируем для отладки
    if current_state:
        logger.warning(f"Unhandled message in state {current_state} from user {message.chat.id}: {message.text[:50] if message.text else '[no text]'}")
        return

    # Пользователь не в FSM-состоянии — подсказываем команды
    intern = await get_intern(message.chat.id)

    if not intern:
        # Новый пользователь
        await message.answer(
            "Здравствуйте! Для начала используйте /start"
        )
    else:
        # Зарегистрированный пользователь
        await message.answer(
            "Используйте команды:\n"
            "/learn — получить тему\n"
            "/progress — мой прогресс\n"
            "/profile — мой профиль\n"
            "/help — справка"
        )

# ============= ЗАПУСК =============

async def main():
    global _dispatcher

    # Инициализация БД
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

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

    # Установка команд бота (Menu-кнопка)
    # /learn вверху - самая частая команда
    await bot.set_my_commands([
        BotCommand(command="learn", description="Получить новую тему"),
        BotCommand(command="progress", description="Мой прогресс"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="update", description="Обновить профиль"),
        BotCommand(command="mode", description="Выбор режима (Марафон/Лента)"),
        BotCommand(command="start", description="Перезапустить онбординг"),
        BotCommand(command="help", description="Справка")
    ])

    # Запуск планировщика
    scheduler.add_job(scheduled_check, 'cron', minute='*')
    scheduler.start()

    logger.info("🚀 Бот запущен с PostgreSQL!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
