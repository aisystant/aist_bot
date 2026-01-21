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

from locales import t, detect_language, get_language_name, SUPPORTED_LANGUAGES
from core.intent import detect_intent, IntentType
from engines.shared import handle_question, ProcessingStage

# Импорты из единого источника (config/, clients/, core/)
from config import (
    BOT_TOKEN,
    ANTHROPIC_API_KEY,
    DATABASE_URL,
    MCP_URL,
    KNOWLEDGE_MCP_URL,
    validate_env,
    get_logger,
    MOSCOW_TZ,
    DIFFICULTY_LEVELS,
    LEARNING_STYLES,
    EXPERIENCE_LEVELS,
    STUDY_DURATIONS,
    BLOOM_LEVELS,
    BLOOM_AUTO_UPGRADE_AFTER,
    DAILY_TOPICS_LIMIT,
    MAX_TOPICS_PER_DAY,
    MARATHON_DAYS,
    ONTOLOGY_RULES,
    TOPICS_DIR,
)
from clients import claude, mcp_guides, mcp_knowledge, mcp
from core.helpers import load_topic_metadata, get_search_keys, get_bloom_questions

# Валидация переменных окружения
validate_env()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def moscow_now() -> datetime:
    """Получить текущее время по Москве"""
    return datetime.now(MOSCOW_TZ)

def moscow_today():
    """Получить текущую дату по Москве"""
    return moscow_now().date()

# ============= КОНСТАНТЫ =============
# Все константы импортированы из config/

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

        # Язык интерфейса
        await conn.execute("ALTER TABLE interns ADD COLUMN IF NOT EXISTS language VARCHAR(5) DEFAULT 'ru'")

        # Второе напоминание
        await conn.execute("ALTER TABLE interns ADD COLUMN IF NOT EXISTS schedule_time_2 TEXT DEFAULT NULL")

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
                'schedule_time_2': row['schedule_time_2'] if 'schedule_time_2' in row.keys() else None,
                'current_topic_index': row['current_topic_index'],
                'completed_topics': json.loads(row['completed_topics']),
                'bloom_level': row['bloom_level'] if row['bloom_level'] else 1,
                'topics_at_current_bloom': row['topics_at_current_bloom'] if row['topics_at_current_bloom'] else 0,
                'topics_today': row['topics_today'] if row['topics_today'] else 0,
                'last_topic_date': row['last_topic_date'],
                'topic_order': row['topic_order'] if 'topic_order' in row.keys() else 'default',
                'marathon_start_date': row['marathon_start_date'] if 'marathon_start_date' in row.keys() else None,
                'onboarding_completed': row['onboarding_completed'],
                'language': row['language'] if 'language' in row.keys() else 'ru'
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
                'schedule_time_2': None,
                'current_topic_index': 0,
                'completed_topics': [],
                'bloom_level': 1,
                'topics_at_current_bloom': 0,
                'topics_today': 0,
                'last_topic_date': None,
                'topic_order': 'default',
                'marathon_start_date': None,
                'onboarding_completed': False,
                'language': 'ru'
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
    # Определяем тип ответа
    if answer.startswith('[РП]'):
        answer_type = 'work_product'
    elif answer.startswith('[BONUS]'):
        answer_type = 'bonus_answer'
    else:
        answer_type = 'theory_answer'

    async with db_pool.acquire() as conn:
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

# ============= КЛИЕНТЫ =============
# claude, mcp_guides, mcp_knowledge, mcp импортированы из clients/


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

def kb_experience(lang: str = 'ru') -> InlineKeyboardMarkup:
    emojis = {'student': '🎓', 'junior': '🌱', 'middle': '💼', 'senior': '⭐', 'switching': '🔄'}
    keys = ['student', 'junior', 'middle', 'senior', 'switching']
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{emojis[k]} {t(f'experience.{k}', lang)}", callback_data=f"exp_{k}")]
        for k in keys
    ])

def kb_difficulty(lang: str = 'ru') -> InlineKeyboardMarkup:
    emojis = {'easy': '🌱', 'medium': '🌿', 'hard': '🌳'}
    keys = ['easy', 'medium', 'hard']
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{emojis[k]} {t(f'difficulty.{k}', lang)}", callback_data=f"diff_{k}")]
        for k in keys
    ])

def kb_learning_style(lang: str = 'ru') -> InlineKeyboardMarkup:
    emojis = {'theoretical': '📚', 'practical': '🔧', 'mixed': '⚖️'}
    keys = ['theoretical', 'practical', 'mixed']
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{emojis[k]} {t(f'learning_style.{k}', lang)}", callback_data=f"style_{k}")]
        for k in keys
    ])

def kb_study_duration(lang: str = 'ru') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(f'duration.minutes_{k}', lang), callback_data=f"duration_{k}")]
        for k in [5, 15, 25]
    ])

def kb_confirm(lang: str = 'ru') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t('buttons.yes', lang), callback_data="confirm"),
            InlineKeyboardButton(text="🔄", callback_data="restart")
        ]
    ])

def kb_learn(lang: str = 'ru') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('buttons.start_now', lang), callback_data="learn")],
        [InlineKeyboardButton(text=t('buttons.start_scheduled', lang), callback_data="later")]
    ])

def kb_update_profile(lang: str = 'ru') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 " + t('buttons.name', lang), callback_data="upd_name"),
         InlineKeyboardButton(text="💼 " + t('buttons.occupation', lang), callback_data="upd_occupation")],
        [InlineKeyboardButton(text="🎨 " + t('buttons.interests', lang), callback_data="upd_interests"),
         InlineKeyboardButton(text="🎯 " + t('buttons.goals', lang), callback_data="upd_goals")],
        [InlineKeyboardButton(text="⏱ " + t('buttons.duration', lang), callback_data="upd_duration"),
         InlineKeyboardButton(text="⏰ " + t('buttons.schedule', lang), callback_data="upd_schedule")],
        [InlineKeyboardButton(text="📊 " + t('buttons.difficulty', lang), callback_data="upd_bloom"),
         InlineKeyboardButton(text="🤖 " + t('buttons.bot_mode', lang), callback_data="upd_mode")],
        [InlineKeyboardButton(text="🌐 Language (en, es, ru)", callback_data="upd_language")],
        [InlineKeyboardButton(text="❌ " + t('buttons.cancel', lang), callback_data="upd_cancel")]
    ])

def kb_bloom_level(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура для выбора уровня сложности"""
    emojis = {1: '🔵', 2: '🟡', 3: '🔴'}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{emojis[k]} {t(f'bloom.level_{k}_short', lang)} — {t(f'bloom.level_{k}_desc', lang)}",
            callback_data=f"bloom_{k}"
        )]
        for k in [1, 2, 3]
    ])

def kb_bonus_question(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура для предложения дополнительного вопроса"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('buttons.bonus_yes', lang), callback_data="bonus_yes")],
        [InlineKeyboardButton(text=t('buttons.bonus_no', lang), callback_data="bonus_no")]
    ])

def kb_skip_topic(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой пропуска темы"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('buttons.skip_topic', lang), callback_data="skip_topic")]
    ])

def kb_marathon_start(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура для выбора даты старта марафона"""
    today = moscow_today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    # Названия дней на разных языках
    day_names = {
        'ru': ('Сегодня', 'Завтра', 'Послезавтра'),
        'en': ('Today', 'Tomorrow', 'Day after'),
        'es': ('Hoy', 'Mañana', 'Pasado mañana')
    }
    names = day_names.get(lang, day_names['en'])

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚀 {names[0]}", callback_data="start_today")],
        [InlineKeyboardButton(text=f"📅 {names[1]} ({tomorrow.strftime('%d.%m')})", callback_data="start_tomorrow")],
        [InlineKeyboardButton(text=f"📅 {names[2]} ({day_after.strftime('%d.%m')})", callback_data="start_day_after")]
    ])

def kb_submit_work_product(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура для практического задания"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('buttons.skip_practice', lang), callback_data="skip_practice")]
    ])

def kb_language_select() -> InlineKeyboardMarkup:
    """Клавиатура для выбора языка интерфейса"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_language_name(lang), callback_data=f"lang_{lang}")]
        for lang in SUPPORTED_LANGUAGES
    ] + [[InlineKeyboardButton(text="❌", callback_data="upd_cancel")]])

def progress_bar(completed: int, total: int) -> str:
    pct = int((completed / total) * 100) if total > 0 else 0
    # Показываем хотя бы 1 заполненный кубик, если есть прогресс
    filled = max(1, pct // 10) if pct > 0 else 0
    return f"{'█' * filled}{'░' * (10 - filled)} {pct}%"

# ============= РОУТЕР =============

router = Router()

# --- Онбординг ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)

    if intern['onboarding_completed']:
        lang = intern.get('language', 'ru')
        await message.answer(
            t('welcome.returning', lang, name=intern['name']) + "\n\n" +
            t('commands.learn', lang) + "\n" +
            t('commands.progress', lang) + "\n" +
            t('commands.profile', lang) + "\n" +
            t('commands.update', lang) + "\n" +
            t('commands.mode', lang)
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
        f"📅 {MARATHON_DAYS} дней — по 2 темы в день (урок + задание)\n"
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
    """Короткий отчёт прогресса за текущую неделю"""
    from db.queries.answers import (
        get_weekly_marathon_stats, get_weekly_feed_stats,
        get_work_products_by_day
    )
    from db.queries.activity import get_activity_stats

    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        await message.answer("Сначала /start")
        return

    chat_id = message.chat.id

    try:
        # Получаем статистику
        activity_stats = await get_activity_stats(chat_id)
        marathon_stats = await get_weekly_marathon_stats(chat_id)
        feed_stats = await get_weekly_feed_stats(chat_id)
        wp_by_day = await get_work_products_by_day(chat_id, TOPICS)
    except Exception as e:
        logger.error(f"Ошибка получения статистики для {chat_id}: {e}")
        activity_stats = {'days_active_this_week': 0}
        marathon_stats = {'work_products': 0}
        feed_stats = {'digests': 0, 'fixations': 0}
        wp_by_day = {}

    # Общие данные
    days_active_week = activity_stats.get('days_active_this_week', 0)

    # Марафон
    done = len(intern['completed_topics'])
    total = get_total_topics()
    marathon_day = get_marathon_day(intern)
    days_progress = get_days_progress(intern['completed_topics'], marathon_day)

    # Формируем прогресс по дням (в обратном порядке, от текущего к первому)
    days_to_show = []
    for d in days_progress:
        day_num = d['day']
        if day_num > marathon_day + 1:
            break
        days_to_show.append(d)

    days_text = ""
    for d in reversed(days_to_show):  # Обратный порядок
        day_num = d['day']
        wp_count = wp_by_day.get(day_num, 0)

        if d['status'] == 'completed':
            emoji = "✅"
            wp_text = f" | РП: {wp_count}" if wp_count > 0 else ""
        elif d['status'] == 'in_progress':
            emoji = "🔄"
            wp_text = f" | РП: {wp_count}" if wp_count > 0 else ""
        elif d['status'] == 'available':
            emoji = "📍"
            wp_text = ""
        else:
            emoji = "🔒"
            wp_text = ""

        status_text = f"{d['completed']}/{d['total']}" if d['status'] != 'locked' else "—/2"
        days_text += f"   {emoji} День {day_num}: {status_text}{wp_text}\n"

    # Лента - получаем темы
    try:
        from engines.feed.engine import FeedEngine
        feed_engine = FeedEngine(chat_id)
        feed_status = await feed_engine.get_status()
        feed_topics = feed_status.get('topics', [])
        feed_topics_text = ", ".join(feed_topics) if feed_topics else "не выбраны"
    except Exception as e:
        logger.error(f"Ошибка получения статуса ленты для {chat_id}: {e}")
        feed_topics_text = "не удалось загрузить"

    # Общие РП за неделю
    total_wp_week = marathon_stats.get('work_products', 0)

    text = f"📊 *Прогресс: {intern['name']}*\n\n"
    text += f"Активных дней за неделю: {days_active_week}\n\n"

    # Марафон
    text += f"🏃 *Марафон* (день {marathon_day}/{MARATHON_DAYS})\n"
    text += f"Пройдено тем: {done}. Рабочих продуктов: {total_wp_week}\n\n"
    text += f"📋 По дням:\n{days_text}\n"

    # Лента
    text += f"📚 *Лента*\n"
    text += f"Дайджестов: {feed_stats.get('digests', 0)}. Фиксаций: {feed_stats.get('fixations', 0)}\n"
    text += f"Темы: {feed_topics_text}"

    # Кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Полный отчёт", callback_data="progress_full"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="go_update")
        ]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data == "progress_full")
async def show_full_progress(callback: CallbackQuery):
    """Полный отчёт с начала использования бота"""
    await callback.answer()  # Сразу отвечаем, чтобы убрать "крутилку" с кнопки

    try:
        from db.queries.answers import get_total_stats

        chat_id = callback.message.chat.id
        intern = await get_intern(chat_id)

        if not intern:
            await callback.message.edit_text("Профиль не найден. Используйте /start")
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
        done = len(intern.get('completed_topics', []))
        total = get_total_topics()
        marathon_day = get_marathon_day(intern)
        pct = int((done / total) * 100) if total > 0 else 0
        filled = max(1, pct // 5) if pct > 0 else 0
        bar = '█' * filled + '░' * (20 - filled)

        # Прогресс по неделям
        weeks = get_sections_progress(intern.get('completed_topics', []))
        weeks_text = ""
        for i, week in enumerate(weeks):
            w_pct = int((week['completed'] / week['total']) * 100) if week['total'] > 0 else 0
            w_filled = max(1, w_pct // 10) if w_pct > 0 else 0
            w_bar = '█' * w_filled + '░' * (10 - w_filled)
            weeks_text += f"{'1️⃣' if i == 0 else '2️⃣'} {w_bar} {week['completed']}/{week['total']}\n"

        # Лента
        try:
            from engines.feed.engine import FeedEngine
            feed_engine = FeedEngine(chat_id)
            feed_status = await feed_engine.get_status()
            feed_topics = feed_status.get('topics', [])
            feed_topics_text = ", ".join(feed_topics) if feed_topics else "не выбраны"
        except Exception as e:
            logger.error(f"Ошибка получения feed_status: {e}")
            feed_topics_text = "—"

        name = intern.get('name', 'Пользователь')
        text = f"📊 *Полный отчёт с {date_str}: {name}*\n\n"
        text += f"Активных дней: {total_active} из {days_since}\n\n"

        # Марафон
        text += f"🏃 *Марафон*\n"
        text += f"День {marathon_day} из {MARATHON_DAYS} | {done}/{total} тем\n"
        text += f"{bar}\n"
        text += f"Рабочих продуктов: {total_stats.get('total_work_products', 0)}\n\n"
        text += f"{weeks_text}\n"

        # Лента
        text += f"📚 *Лента*\n"
        text += f"Дайджестов: {total_stats.get('total_digests', 0)}. Фиксаций: {total_stats.get('total_fixations', 0)}\n"
        text += f"Темы: {feed_topics_text}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="progress_back")]
        ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка в show_full_progress: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.message.edit_text(
            "Не удалось загрузить полный отчёт. Попробуйте позже.\n\n/progress — вернуться"
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
async def go_to_update(callback: CallbackQuery):
    """Переход к настройкам"""
    await callback.answer()
    # Имитируем команду /update
    await callback.message.delete()
    await callback.message.answer("/update — настройки профиля")

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
        f"📖 *{t('help.title', lang)}:*\n\n"
        f"{t('commands.learn', lang)}\n"
        f"/mode — {t('menu.mode', lang)}\n"
        f"{t('commands.progress', lang)}\n"
        f"{t('commands.profile', lang)}\n"
        f"{t('commands.update', lang)}\n"
        f"{t('commands.language', lang)}\n\n"
        f"*{t('help.how_it_works', lang)}:*\n"
        f"{t('help.step1', lang)}\n"
        f"{t('help.step2', lang)}\n"
        f"{t('help.step3', lang)}\n"
        f"{t('help.step4', lang)}\n\n"
        f"{t('help.schedule_note', lang)}\n\n"
        "🔗 [Мастерская инженеров-менеджеров](https://system-school.ru/)\n\n"
        f"💬 {t('help.feedback', lang)}: @tserentserenov",
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
    lang = intern.get('language', 'ru')

    if not intern['onboarding_completed']:
        await message.answer(t('errors.try_again', lang) + " /start")
        return

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
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer(t('buttons.cancel', lang))
    await callback.message.edit_text(t('commands.learn', lang))
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
            next_topic_hint = f"\n\n📝 *{t('marathon.next_task', lang)}:* {next_topic['title']}"
            next_command = t('marathon.continue_to_task', lang)
        else:
            next_topic_hint = f"\n\n📚 *{t('marathon.next_lesson', lang)}:* {next_topic['title']}"
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
        await message.answer(
            f"✅ *{t('marathon.topic_completed', lang)}*\n\n"
            f"{progress_bar(done, total)}\n"
            f"{t(f'bloom.level_{bloom_level}_short', lang)}{upgrade_msg}{next_topic_hint}\n\n"
            f"{next_command}",
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
        question = await claude.generate_question(topic, intern, marathon_day=marathon_day, bloom_level=next_level)

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
async def on_bonus_no(callback: CallbackQuery, state: FSMContext):
    """Пользователь отказался от дополнительного вопроса"""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    data = await state.get_data()
    next_command = data.get('next_command', t('marathon.next_command', lang))
    await callback.answer(t('marathon.ok', lang))
    await callback.message.edit_text(
        callback.message.text + f"\n\n{next_command}",
        parse_mode="Markdown"
    )
    await state.clear()

@router.message(LearningStates.waiting_for_bonus_answer)
async def on_bonus_answer(message: Message, state: FSMContext):
    """Обработка ответа на бонусный вопрос"""
    chat_id = message.chat.id
    current_state = await state.get_state()
    logger.info(f"[BONUS] on_bonus_answer вызван для chat_id={chat_id}, state={current_state}")

    if len(message.text.strip()) < 20:
        await message.answer("Напишите подробнее (хотя бы 2-3 предложения)")
        return

    intern = await get_intern(chat_id)
    data = await state.get_data()
    topic_index = data.get('topic_index', 0)
    lang = intern.get('language', 'ru') if intern else 'ru'
    logger.info(f"[BONUS] Processing answer: topic_index={topic_index}, data_keys={list(data.keys())}")

    try:
        # Сохраняем ответ на бонусный вопрос
        await save_answer(message.chat.id, topic_index, f"[BONUS] {message.text.strip()}")

        bloom_level = intern['bloom_level'] if intern else 1

        # Получаем информацию о следующей доступной теме
        next_available = get_available_topics(intern) if intern else []
        next_topic_hint = ""
        next_command = data.get('next_command', t('marathon.next_command', lang))
        if next_available:
            next_topic = next_available[0][1]  # (index, topic) -> topic
            # Определяем тип следующей темы
            if next_topic.get('type') == 'practice':
                next_topic_hint = f"\n\n📝 *{t('marathon.next_task', lang)}:* {next_topic['title']}"
                next_command = t('marathon.continue_to_task', lang)
            else:
                next_topic_hint = f"\n\n📚 *{t('marathon.next_lesson', lang)}:* {next_topic['title']}"
                next_command = t('marathon.continue_to_lesson', lang)

        await message.answer(
            f"🌟 *{t('marathon.bonus_completed', lang)}*\n\n"
            f"{t('marathon.training_skills', lang)} *{t(f'bloom.level_{bloom_level}_short', lang)}* {t('marathon.and_higher', lang)}{next_topic_hint}\n\n"
            f"{next_command}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка обработки бонусного ответа: {e}")
        next_command = data.get('next_command', t('marathon.next_command', lang))
        await message.answer(
            f"✅ Ответ принят!\n\n{next_command}"
        )
    finally:
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
    topic_title = topic['title'] if topic else "задание"

    await callback.answer("Задание пропущено")
    await callback.message.edit_text(
        f"⏭ *Задание пропущено:* {topic_title}\n\n"
        f"_Пропущенные задания не засчитываются в прогресс._\n\n"
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
                filled = max(1, pct // 10) if pct > 0 else 0
                bar = '█' * filled + '░' * (10 - filled)
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
    lang = intern.get('language', 'ru')
    bloom_level = intern['bloom_level']

    # Показываем, что бот работает
    await bot.send_chat_action(chat_id=chat_id, action="typing")
    await bot.send_message(chat_id, f"⏳ {t('marathon.generating_material', lang)}")

    content = await claude.generate_content(topic, intern, marathon_day=marathon_day, mcp_client=mcp_guides, knowledge_client=mcp_knowledge)
    question = await claude.generate_question(topic, intern, marathon_day=marathon_day)

    header = (
        f"📚 *{t('marathon.day_theory', lang, day=marathon_day)}*\n"
        f"*{topic['title']}*\n"
        f"⏱ {t('marathon.minutes', lang, minutes=intern['study_duration'])}\n\n"
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
        f"💭 *{t('marathon.reflection_question', lang)}* ({t(f'bloom.level_{bloom_level}_short', lang)})\n\n"
        f"{question}\n\n"
        f"_{t('marathon.answer_hint', lang)}_",
        parse_mode="Markdown",
        reply_markup=kb_skip_topic(lang)
    )

    await state.set_state(LearningStates.waiting_for_answer)


async def send_practice_topic(chat_id: int, topic: dict, intern: dict, state: FSMContext, bot: Bot):
    """Отправка практической темы"""
    marathon_day = get_marathon_day(intern)
    lang = intern.get('language', 'ru')

    # Показываем, что бот работает
    await bot.send_chat_action(chat_id=chat_id, action="typing")
    await bot.send_message(chat_id, f"⏳ {t('marathon.preparing_practice', lang)}")

    # Генерируем краткое введение
    intro = await claude.generate_practice_intro(topic, intern, marathon_day=marathon_day)

    task = topic.get('task', '')
    work_product = topic.get('work_product', '')
    examples = topic.get('work_product_examples', [])

    examples_text = ""
    if examples:
        examples_text = f"\n*{t('marathon.wp_examples', lang)}:*\n" + "\n".join([f"• {ex}" for ex in examples])

    header = (
        f"✏️ *{t('marathon.day_practice', lang, day=marathon_day)}*\n"
        f"*{topic['title']}*\n\n"
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

    # Запрос рабочего продукта
    await bot.send_message(
        chat_id,
        f"📝 *{t('marathon.when_complete', lang)}:*\n\n"
        f"{t('marathon.write_wp_name', lang)}\n\n"
        f"_{t('marathon.example', lang)}: «{examples[0] if examples else work_product}»_\n\n"
        f"_{t('marathon.no_check_hint', lang)}_",
        parse_mode="Markdown",
        reply_markup=kb_submit_work_product(lang)
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
                filled = max(1, pct // 10) if pct > 0 else 0
                bar = '█' * filled + '░' * (10 - filled)
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
            f"Всего 2 темы на сегодня: урок и задание.\n\n"
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
    text = message.text or ''
    chat_id = message.chat.id
    logger.info(f"[UNKNOWN] on_unknown_message вызван для chat_id={chat_id}, state={current_state}, text={text[:50] if text else '[no text]'}")

    # Если пользователь в каком-то состоянии — логируем для отладки
    if current_state:
        logger.warning(f"Unhandled message in state {current_state} from user {chat_id}: {text[:50] if text else '[no text]'}")
        return

    # Пользователь не в FSM-состоянии
    logger.info(f"[UNKNOWN] Пользователь {chat_id} не в FSM-состоянии, проверяем intent")
    intern = await get_intern(chat_id)

    if not intern:
        # Новый пользователь
        await message.answer(
            "Здравствуйте! Для начала используйте /start"
        )
        return

    # Определяем намерение пользователя
    intent = detect_intent(text, context={'mode': intern.get('mode')})
    lang = intern.get('language', 'ru') or 'ru'

    if intent.type == IntentType.QUESTION:
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
            answer, sources = await handle_question(
                question=text,
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

    elif intent.type == IntentType.TOPIC_REQUEST:
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

    # Установка команд бота для разных языков
    # Русский (по умолчанию)
    await bot.set_my_commands([
        BotCommand(command="learn", description="Получить новую тему"),
        BotCommand(command="progress", description="Мой прогресс"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="update", description="Обновить профиль"),
        BotCommand(command="mode", description="Выбор режима"),
        BotCommand(command="language", description="Change language"),
        BotCommand(command="start", description="Перезапустить онбординг"),
        BotCommand(command="help", description="Справка")
    ])

    # Английский
    await bot.set_my_commands([
        BotCommand(command="learn", description="Get a new topic"),
        BotCommand(command="progress", description="My progress"),
        BotCommand(command="profile", description="My profile"),
        BotCommand(command="update", description="Update profile"),
        BotCommand(command="mode", description="Select mode"),
        BotCommand(command="language", description="Change language"),
        BotCommand(command="start", description="Restart onboarding"),
        BotCommand(command="help", description="Help")
    ], language_code="en")

    # Испанский
    await bot.set_my_commands([
        BotCommand(command="learn", description="Obtener tema"),
        BotCommand(command="progress", description="Mi progreso"),
        BotCommand(command="profile", description="Mi perfil"),
        BotCommand(command="update", description="Actualizar perfil"),
        BotCommand(command="mode", description="Seleccionar modo"),
        BotCommand(command="language", description="Change language"),
        BotCommand(command="start", description="Reiniciar"),
        BotCommand(command="help", description="Ayuda")
    ], language_code="es")

    # Запуск планировщика
    scheduler.add_job(scheduled_check, 'cron', minute='*')
    scheduler.start()

    logger.info("🚀 Бот запущен с PostgreSQL!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
