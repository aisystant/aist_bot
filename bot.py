"""
AIST Pilot Bot — Telegram-бот для персонального обучения стажера
GitHub: https://github.com/aisystant/aist_pilot_bot

С поддержкой PostgreSQL для хранения данных пользователей.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import aiohttp
import asyncpg

# ============= КОНФИГУРАЦИЯ =============

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MCP_URL = os.getenv("MCP_URL", "https://guides-mcp.aisystant.workers.dev/mcp")

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
    "10": {"emoji": "🕐", "name": "10 минут", "words": 1000, "desc": "Краткое изучение"},
    "15": {"emoji": "🕑", "name": "15 минут", "words": 1500, "desc": "Стандартное изучение"},
    "20": {"emoji": "🕒", "name": "20 минут", "words": 2000, "desc": "Углублённое изучение"},
    "25": {"emoji": "🕓", "name": "25 минут", "words": 2500, "desc": "Полное погружение"}
}

# Уровни сложности по таксономии Блума (сгруппированы в 3 уровня)
BLOOM_LEVELS = {
    1: {
        "emoji": "🔵",
        "name": "Понимание",
        "desc": "Запоминание и понимание концепций",
        "question_type": "Объясни своими словами, что такое {concept}? Приведи пример из своей области.",
        "prompt": "Создай вопрос на ПОНИМАНИЕ темы. Попроси объяснить концепцию своими словами или привести пример."
    },
    2: {
        "emoji": "🟡",
        "name": "Применение",
        "desc": "Применение и анализ в практике",
        "question_type": "Как бы ты применил {concept} в своей работе? Разбери конкретную ситуацию.",
        "prompt": "Создай вопрос на ПРИМЕНЕНИЕ темы. Попроси применить концепцию к конкретной рабочей ситуации стажера или проанализировать кейс."
    },
    3: {
        "emoji": "🔴",
        "name": "Создание",
        "desc": "Оценка и создание нового",
        "question_type": "Предложи своё решение на основе {concept}. Оцени плюсы и минусы разных подходов.",
        "prompt": "Создай вопрос на СОЗДАНИЕ/ОЦЕНКУ. Попроси предложить своё решение, оценить подходы или создать план действий на основе изученного."
    }
}

# Автоматическое повышение уровня: после N тем на текущем уровне
BLOOM_AUTO_UPGRADE_AFTER = 7  # после 7 тем уровень повышается

# Лимит тем в день (для развития систематичности)
DAILY_TOPICS_LIMIT = 2

# ============= СОСТОЯНИЯ FSM =============

class OnboardingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_role = State()
    waiting_for_domain = State()
    waiting_for_interests = State()
    waiting_for_problems = State()
    waiting_for_desires = State()
    waiting_for_experience = State()
    waiting_for_difficulty = State()
    waiting_for_learning_style = State()
    waiting_for_study_duration = State()
    waiting_for_goals = State()
    waiting_for_schedule = State()
    confirming_profile = State()

class LearningStates(StatesGroup):
    waiting_for_answer = State()
    waiting_for_bonus_answer = State()  # ответ на дополнительный вопрос посложнее

class UpdateStates(StatesGroup):
    choosing_field = State()
    updating_problems = State()
    updating_desires = State()
    updating_goals = State()
    updating_bloom_level = State()  # смена уровня сложности Блума

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
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS answers (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                topic_index INTEGER,
                answer TEXT,
                created_at TIMESTAMP DEFAULT NOW()
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
                'role': row['role'],
                'domain': row['domain'],
                'interests': json.loads(row['interests']),
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
                'role': '',
                'domain': '',
                'interests': [],
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
            'SELECT chat_id, name FROM interns WHERE schedule_time = $1 AND onboarding_completed = TRUE',
            time_str
        )
        return [{'chat_id': row['chat_id'], 'name': row['name']} for row in rows]

def get_topics_today(intern: dict) -> int:
    """Получить количество тем, пройденных сегодня"""
    today = datetime.now().date()
    last_date = intern.get('last_topic_date')

    # Если last_topic_date — это дата сегодня, возвращаем topics_today
    if last_date and last_date == today:
        return intern.get('topics_today', 0)
    # Иначе — новый день, счётчик обнуляется
    return 0

def get_personalization_prompt(intern: dict) -> str:
    """Генерирует промпт для персонализации"""
    diff = DIFFICULTY_LEVELS.get(intern['difficulty_preference'], {})
    style = LEARNING_STYLES.get(intern['learning_style'], {})
    exp = EXPERIENCE_LEVELS.get(intern['experience_level'], {})
    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {"words": 1500})

    interests = ', '.join(intern['interests']) if intern['interests'] else 'не указаны'
    problems = intern.get('current_problems', '') or 'не указаны'
    desires = intern.get('desires', '') or 'не указаны'

    return f"""
ПРОФИЛЬ СТАЖЕРА:
- Имя: {intern['name']}
- Роль: {intern['role']}
- Предметная область: {intern['domain']}
- Интересы: {interests}
- Текущие проблемы/боли: {problems}
- Желания/чего хочет достичь: {desires}
- Уровень опыта: {exp.get('name', '')} ({exp.get('desc', '')})
- Желаемая сложность: {diff.get('name', '')} ({diff.get('desc', '')})
- Стиль обучения: {style.get('name', '')} ({style.get('desc', '')})
- Время на изучение: {intern['study_duration']} минут (~{duration.get('words', 1500)} слов)
- Цели обучения: {intern['goals']}

ИНСТРУКЦИИ:
1. Используй примеры из области "{intern['domain']}" и интересов стажера
2. Связывай материал с текущими проблемами стажера — показывай, как тема помогает их решить
3. Показывай, как изучение темы приближает к желаемым результатам стажера
4. Адаптируй сложность под уровень "{diff.get('name', 'средний')}"
5. {'Начинай с теории' if intern['learning_style'] == 'theoretical' else 'Начинай с практических примеров' if intern['learning_style'] == 'practical' else 'Чередуй теорию и практику'}
6. Объём текста должен быть рассчитан на {intern['study_duration']} минут чтения (~{duration.get('words', 1500)} слов)
"""

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

    async def generate_content(self, topic: dict, intern: dict, mcp_client=None) -> str:
        duration = STUDY_DURATIONS.get(str(intern['study_duration']), {"words": 1500})
        words = duration.get('words', 1500)

        # Получаем контекст из MCP (semantic search по теме)
        mcp_context = ""
        if mcp_client:
            try:
                search_query = f"{topic.get('title')} {topic.get('main_concept')}"
                search_results = await mcp_client.semantic_search(search_query, lang="ru", limit=3)

                if search_results:
                    context_parts = []
                    for item in search_results[:3]:
                        if isinstance(item, dict):
                            text = item.get('text', item.get('content', ''))
                            if text:
                                # Ограничиваем длину каждого фрагмента
                                context_parts.append(text[:1500])
                        elif isinstance(item, str):
                            context_parts.append(item[:1500])

                    if context_parts:
                        mcp_context = "\n\n---\n\n".join(context_parts)
                        logger.info(f"MCP: найдено {len(context_parts)} фрагментов контекста")
            except Exception as e:
                logger.error(f"MCP search error: {e}")

        system_prompt = f"""Ты — персональный наставник по системному мышлению и личному развитию.
{get_personalization_prompt(intern)}

Создай текст на {intern['study_duration']} минут чтения (~{words} слов). Без заголовков, только абзацы.
Текст должен быть вовлекающим, с примерами из жизни читателя.
{"Используй предоставленный контекст из руководств Aisystant как основу для материала." if mcp_context else ""}"""

        # Формируем контекст из структуры знаний
        pain_point = topic.get('pain_point', '')
        key_insight = topic.get('key_insight', '')
        source = topic.get('source', '')

        user_prompt = f"""Тема: {topic.get('title')}
Основное понятие: {topic.get('main_concept')}
Связанные понятия: {', '.join(topic.get('related_concepts', []))}

{"Боль читателя: " + pain_point if pain_point else ""}
{"Ключевой инсайт: " + key_insight if key_insight else ""}
{"Источник: " + source if source else ""}

{f"КОНТЕКСТ ИЗ РУКОВОДСТВ AISYSTANT:{chr(10)}{mcp_context}" if mcp_context else ""}

Начни с признания боли читателя, затем раскрой тему и подведи к ключевому инсайту.
{"Опирайся на контекст из руководств, но адаптируй под профиль стажера." if mcp_context else ""}"""

        result = await self.generate(system_prompt, user_prompt)
        return result or "Не удалось сгенерировать контент. Попробуйте /learn ещё раз."

    async def generate_question(self, topic: dict, intern: dict, bloom_level: int = None) -> str:
        """Генерирует вопрос по теме с учётом уровня Блума"""
        level = bloom_level or intern.get('bloom_level', 1)
        bloom = BLOOM_LEVELS.get(level, BLOOM_LEVELS[1])

        system_prompt = f"""Создай один вопрос для проверки понимания темы.
{get_personalization_prompt(intern)}

УРОВЕНЬ СЛОЖНОСТИ ВОПРОСА: {bloom['name']} ({bloom['desc']})
{bloom['prompt']}

Вопрос должен требовать развёрнутого ответа и быть связан с областью стажера "{intern['domain']}"."""

        user_prompt = f"""Тема: {topic.get('title')}
Понятие: {topic.get('main_concept')}

Создай вопрос уровня "{bloom['name']}" для этой темы."""

        result = await self.generate(system_prompt, user_prompt)
        return result or bloom['question_type'].format(concept=topic.get('main_concept', 'эту тему'))

claude = ClaudeClient()

# ============= MCP CLIENT =============

class MCPClient:
    """Клиент для работы с MCP сервером руководств Aisystant"""

    def __init__(self):
        self.base_url = MCP_URL
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
                            logger.error(f"MCP error: {data['error']}")
                            return None
                    else:
                        error = await resp.text()
                        logger.error(f"MCP HTTP error {resp.status}: {error}")
                        return None
        except asyncio.TimeoutError:
            logger.error("MCP request timeout")
            return None
        except Exception as e:
            logger.error(f"MCP exception: {e}")
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

    async def semantic_search(self, query: str, lang: str = "ru", limit: int = 5) -> List[dict]:
        """Семантический поиск по руководствам"""
        result = await self._call("semantic_search", {
            "query": query,
            "lang": lang,
            "limit": limit
        })
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        return json.loads(item.get("text", "[]"))
                    except json.JSONDecodeError:
                        # Если не JSON, возвращаем как текст
                        return [{"text": item.get("text", "")}]
        return []

mcp = MCPClient()

# ============= СТРУКТУРА ЗНАНИЙ =============

def load_knowledge_structure() -> List[dict]:
    """Загружает структуру знаний из YAML файла"""
    yaml_path = Path(__file__).parent / "knowledge_structure.yaml"

    if not yaml_path.exists():
        logger.warning(f"Файл {yaml_path} не найден, используем пустую структуру")
        return []

    with open(yaml_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    # Преобразуем иерархическую структуру в плоский список тем
    topics = []
    for section in data.get('sections', []):
        section_title = section.get('title', '')
        for topic in section.get('topics', []):
            topics.append({
                'id': topic.get('id', ''),
                'section': section_title,
                'subsection': f"Тема {topic.get('order', 0)}",
                'title': topic.get('title', ''),
                'main_concept': topic.get('main_concept', ''),
                'related_concepts': topic.get('related_concepts', []),
                'key_insight': topic.get('key_insight', ''),
                'pain_point': topic.get('pain_point', ''),
                'source': topic.get('source', '')
            })

    # Сортируем по порядку
    topics.sort(key=lambda x: int(x['id'].split('-')[0]) * 100 + int(x['id'].split('-')[1]) if '-' in x['id'] else 0)

    logger.info(f"✅ Загружено {len(topics)} тем из структуры знаний")
    return topics

# Загружаем темы при старте
TOPICS = load_knowledge_structure()

def get_topic(index: int) -> Optional[dict]:
    """Получить тему по индексу"""
    return TOPICS[index] if index < len(TOPICS) else None

def get_total_topics() -> int:
    """Получить общее количество тем"""
    return len(TOPICS)

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
        [InlineKeyboardButton(text="▶️ Начать изучение", callback_data="learn")],
        [InlineKeyboardButton(text="⏭ Позже", callback_data="later")]
    ])

def kb_update_profile() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😓 Проблемы", callback_data="upd_problems")],
        [InlineKeyboardButton(text="✨ Желания", callback_data="upd_desires")],
        [InlineKeyboardButton(text="🎯 Цели", callback_data="upd_goals")],
        [InlineKeyboardButton(text="🎚 Уровень сложности", callback_data="upd_bloom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="upd_cancel")]
    ])

def kb_bloom_level() -> InlineKeyboardMarkup:
    """Клавиатура для выбора уровня Блума"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{v['emoji']} {v['name']}",
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
            f"/profile — твой профиль"
        )
        return
    
    await message.answer(
        "👋 Привет! Я помощник для персонального обучения.\n\n"
        "Задам несколько вопросов, чтобы адаптировать материал под тебя (~2 мин).\n\n"
        "Как тебя зовут?"
    )
    await state.set_state(OnboardingStates.waiting_for_name)

@router.message(OnboardingStates.waiting_for_name)
async def on_name(message: Message, state: FSMContext):
    await update_intern(message.chat.id, name=message.text.strip())
    await message.answer(f"Приятно познакомиться, {message.text.strip()}! 👋\n\nКем ты работаешь или учишься?")
    await state.set_state(OnboardingStates.waiting_for_role)

@router.message(OnboardingStates.waiting_for_role)
async def on_role(message: Message, state: FSMContext):
    await update_intern(message.chat.id, role=message.text.strip())
    await message.answer("В какой предметной области работаешь?\n\nНапример: IT, маркетинг, финансы, дизайн")
    await state.set_state(OnboardingStates.waiting_for_domain)

@router.message(OnboardingStates.waiting_for_domain)
async def on_domain(message: Message, state: FSMContext):
    await update_intern(message.chat.id, domain=message.text.strip())
    await message.answer("Расскажи о своих интересах/хобби?\n\nЭто поможет приводить близкие тебе примеры.")
    await state.set_state(OnboardingStates.waiting_for_interests)

@router.message(OnboardingStates.waiting_for_interests)
async def on_interests(message: Message, state: FSMContext):
    interests = [i.strip() for i in message.text.replace(',', ';').split(';') if i.strip()]
    await update_intern(message.chat.id, interests=interests)
    await message.answer(
        "Какие у тебя сейчас *главные проблемы или сложности*?\n\n"
        "Например: не хватает времени, сложно сосредоточиться, "
        "не знаю с чего начать, выгораю на работе...",
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_problems)

@router.message(OnboardingStates.waiting_for_problems)
async def on_problems(message: Message, state: FSMContext):
    await update_intern(message.chat.id, current_problems=message.text.strip())
    await message.answer(
        "А чего ты *хочешь достичь*? Какой результат был бы для тебя идеальным?\n\n"
        "Например: научиться управлять временем, стать увереннее, "
        "разобраться в системном мышлении, найти своё дело...",
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_desires)

@router.message(OnboardingStates.waiting_for_desires)
async def on_desires(message: Message, state: FSMContext):
    await update_intern(message.chat.id, desires=message.text.strip())
    await message.answer("Какой у тебя уровень опыта?", reply_markup=kb_experience())
    await state.set_state(OnboardingStates.waiting_for_experience)

@router.callback_query(OnboardingStates.waiting_for_experience, F.data.startswith("exp_"))
async def on_experience(callback: CallbackQuery, state: FSMContext):
    level = callback.data.replace("exp_", "")
    await update_intern(callback.message.chat.id, experience_level=level)
    await callback.answer()
    await callback.message.edit_text("Какую сложность материала предпочитаешь?", reply_markup=kb_difficulty())
    await state.set_state(OnboardingStates.waiting_for_difficulty)

@router.callback_query(OnboardingStates.waiting_for_difficulty, F.data.startswith("diff_"))
async def on_difficulty(callback: CallbackQuery, state: FSMContext):
    level = callback.data.replace("diff_", "")
    await update_intern(callback.message.chat.id, difficulty_preference=level)
    await callback.answer()
    await callback.message.edit_text("Как тебе комфортнее учиться?", reply_markup=kb_learning_style())
    await state.set_state(OnboardingStates.waiting_for_learning_style)

@router.callback_query(OnboardingStates.waiting_for_learning_style, F.data.startswith("style_"))
async def on_style(callback: CallbackQuery, state: FSMContext):
    style = callback.data.replace("style_", "")
    await update_intern(callback.message.chat.id, learning_style=style)
    await callback.answer()
    await callback.message.edit_text(
        "Сколько времени готов уделять изучению одной темы?",
        reply_markup=kb_study_duration()
    )
    await state.set_state(OnboardingStates.waiting_for_study_duration)

@router.callback_query(OnboardingStates.waiting_for_study_duration, F.data.startswith("duration_"))
async def on_duration(callback: CallbackQuery, state: FSMContext):
    duration = int(callback.data.replace("duration_", ""))
    await update_intern(callback.message.chat.id, study_duration=duration)
    await callback.answer()
    await callback.message.edit_text("✅ Принято!")
    await callback.message.answer("Какие цели обучения? Чего хочешь достичь?")
    await state.set_state(OnboardingStates.waiting_for_goals)

@router.message(OnboardingStates.waiting_for_goals)
async def on_goals(message: Message, state: FSMContext):
    await update_intern(message.chat.id, goals=message.text.strip())
    await message.answer("Когда отправлять материал?\n\nНапиши время (например: 09:00)")
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

    await update_intern(message.chat.id, schedule_time=message.text.strip())
    intern = await get_intern(message.chat.id)

    exp = EXPERIENCE_LEVELS.get(intern['experience_level'], {})
    diff = DIFFICULTY_LEVELS.get(intern['difficulty_preference'], {})
    style = LEARNING_STYLES.get(intern['learning_style'], {})
    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})

    problems_short = intern['current_problems'][:100] + '...' if len(intern['current_problems']) > 100 else intern['current_problems']
    desires_short = intern['desires'][:100] + '...' if len(intern['desires']) > 100 else intern['desires']

    await message.answer(
        f"📋 *Твой профиль:*\n\n"
        f"👤 {intern['name']}\n"
        f"💼 {intern['role']}\n"
        f"🎯 {intern['domain']}\n"
        f"🎨 {', '.join(intern['interests'])}\n\n"
        f"😓 *Проблемы:* {problems_short}\n"
        f"✨ *Желания:* {desires_short}\n\n"
        f"{exp.get('emoji','')} {exp.get('name','')}\n"
        f"{diff.get('emoji','')} {diff.get('name','')}\n"
        f"{style.get('emoji','')} {style.get('name','')}\n"
        f"{duration.get('emoji','')} {duration.get('name','')} на тему\n\n"
        f"🎯 {intern['goals']}\n"
        f"⏰ {intern['schedule_time']}\n\n"
        f"Всё верно?",
        parse_mode="Markdown",
        reply_markup=kb_confirm()
    )
    await state.set_state(OnboardingStates.confirming_profile)

@router.callback_query(OnboardingStates.confirming_profile, F.data == "confirm")
async def on_confirm(callback: CallbackQuery, state: FSMContext):
    await update_intern(callback.message.chat.id, onboarding_completed=True)
    intern = await get_intern(callback.message.chat.id)
    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    await callback.answer("Сохранено!")

    # Приветственное сообщение с описанием бота
    await callback.message.edit_text(
        f"🎉 *Добро пожаловать, {intern['name']}!*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Что это за бот?*\n\n"
        f"Я помогу тебе перейти от *случайного саморазвития* "
        f"к *систематическому обучению*.\n\n"
        f"Моя цель — развить у тебя:\n"
        f"• *Системное мировоззрение* — видеть целое и связи\n"
        f"• *Системную грамотность* — владеть инструментами мышления\n"
        f"• *Агентность* — способность действовать и менять реальность\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Как устроено обучение?*\n\n"
        f"📚 *28 тем* в 4 разделах — от проблем к решениям\n"
        f"⏱ *{intern['study_duration']} минут* — на изучение темы\n"
        f"❓ *Вопрос* — для закрепления материала\n"
        f"📈 *{DAILY_TOPICS_LIMIT} темы в день* — тренируем систематичность\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Сложность вопросов*\n\n"
        f"Сейчас: {bloom['emoji']} *{bloom['name']}*\n\n"
        f"Сложность растёт автоматически по мере прогресса.\n"
        f"Можно изменить вручную: /update → Уровень сложности\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏰ Буду напоминать в *{intern['schedule_time']}* каждый день.\n\n"
        f"Готов начать?",
        parse_mode="Markdown",
        reply_markup=kb_learn()
    )
    await state.clear()

@router.callback_query(OnboardingStates.confirming_profile, F.data == "restart")
async def on_restart(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("Давай заново!\n\nКак тебя зовут?")
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
    await callback.message.edit_text(f"Жду тебя в {intern['schedule_time']}! Или /learn")

@router.message(Command("progress"))
async def cmd_progress(message: Message):
    intern = await get_intern(message.chat.id)
    if not intern['onboarding_completed']:
        await message.answer("Сначала /start")
        return
    
    done = len(intern['completed_topics'])
    total = get_total_topics()
    await message.answer(
        f"📊 *{intern['name']}*\n\n"
        f"✅ {done} из {total} тем\n"
        f"{progress_bar(done, total)}\n\n"
        f"/learn — продолжить",
        parse_mode="Markdown"
    )

@router.message(Command("profile"))
async def cmd_profile(message: Message):
    intern = await get_intern(message.chat.id)
    if not intern['onboarding_completed']:
        await message.answer("Сначала /start")
        return

    exp = EXPERIENCE_LEVELS.get(intern['experience_level'], {})
    diff = DIFFICULTY_LEVELS.get(intern['difficulty_preference'], {})
    style = LEARNING_STYLES.get(intern['learning_style'], {})
    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    problems_short = intern['current_problems'][:100] + '...' if len(intern['current_problems']) > 100 else intern['current_problems']
    desires_short = intern['desires'][:100] + '...' if len(intern['desires']) > 100 else intern['desires']

    await message.answer(
        f"👤 *{intern['name']}*\n"
        f"💼 {intern['role']}\n"
        f"🎯 {intern['domain']}\n"
        f"🎨 {', '.join(intern['interests'])}\n\n"
        f"😓 *Проблемы:* {problems_short}\n"
        f"✨ *Желания:* {desires_short}\n\n"
        f"{exp.get('emoji','')} {exp.get('name','')}\n"
        f"{diff.get('emoji','')} {diff.get('name','')}\n"
        f"{style.get('emoji','')} {style.get('name','')}\n"
        f"{duration.get('emoji','')} {duration.get('name','')} на тему\n"
        f"{bloom['emoji']} Уровень вопросов: {bloom['name']}\n\n"
        f"🎯 {intern['goals']}\n"
        f"⏰ Обучение в {intern['schedule_time']}\n\n"
        f"/update — обновить профиль",
        parse_mode="Markdown"
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 *Доступные команды:*\n\n"
        "/start — начать или перезапустить онбординг\n"
        "/learn — получить новую тему для изучения\n"
        "/progress — посмотреть свой прогресс\n"
        "/profile — посмотреть свой профиль\n"
        "/update — обновить проблемы, желания, цели\n"
        "/help — показать эту справку\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "*Как работает обучение:*\n"
        "1. Я отправляю персонализированный материал\n"
        "2. Ты изучаешь его (5-25 мин)\n"
        "3. Отвечаешь на вопрос для закрепления\n"
        "4. Тема засчитывается в прогресс\n\n"
        "Материал буду отправлять в заданное время или по /learn",
        parse_mode="Markdown"
    )

# --- Обновление профиля ---

@router.message(Command("update"))
async def cmd_update(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    if not intern['onboarding_completed']:
        await message.answer("Сначала пройди онбординг: /start")
        return

    await message.answer(
        "Что хочешь обновить?\n\n"
        "Это поможет мне лучше персонализировать материалы под тебя.",
        reply_markup=kb_update_profile()
    )
    await state.set_state(UpdateStates.choosing_field)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_problems")
async def on_upd_problems(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    await callback.answer()
    await callback.message.edit_text(
        f"😓 *Текущие проблемы:*\n{intern['current_problems'] or 'не указаны'}\n\n"
        "Опиши свои актуальные проблемы и сложности:",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_problems)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_desires")
async def on_upd_desires(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    await callback.answer()
    await callback.message.edit_text(
        f"✨ *Текущие желания:*\n{intern['desires'] or 'не указаны'}\n\n"
        "Чего ты хочешь достичь? Какой результат был бы идеальным?",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_desires)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_goals")
async def on_upd_goals(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    await callback.answer()
    await callback.message.edit_text(
        f"🎯 *Текущие цели обучения:*\n{intern['goals'] or 'не указаны'}\n\n"
        "Какие у тебя цели обучения?",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_goals)

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_bloom")
async def on_upd_bloom(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])
    await callback.answer()
    await callback.message.edit_text(
        f"🎚 *Текущий уровень:* {bloom['emoji']} {bloom['name']}\n"
        f"_{bloom['desc']}_\n\n"
        f"Пройдено тем на этом уровне: {intern['topics_at_current_bloom']}/{BLOOM_AUTO_UPGRADE_AFTER}\n\n"
        "Выбери новый уровень сложности вопросов:",
        parse_mode="Markdown",
        reply_markup=kb_bloom_level()
    )
    await state.set_state(UpdateStates.updating_bloom_level)

@router.callback_query(UpdateStates.updating_bloom_level, F.data.startswith("bloom_"))
async def on_save_bloom(callback: CallbackQuery, state: FSMContext):
    level = int(callback.data.replace("bloom_", ""))
    await update_intern(callback.message.chat.id, bloom_level=level, topics_at_current_bloom=0)

    bloom = BLOOM_LEVELS.get(level, BLOOM_LEVELS[1])
    await callback.answer(f"Уровень: {bloom['name']}")
    await callback.message.edit_text(
        f"✅ Уровень сложности изменён на *{bloom['name']}*!\n\n"
        f"{bloom['desc']}\n\n"
        f"/learn — продолжить обучение\n"
        f"/update — обновить ещё что-то",
        parse_mode="Markdown"
    )
    await state.clear()

@router.callback_query(UpdateStates.choosing_field, F.data == "upd_cancel")
async def on_upd_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Отменено")
    await callback.message.edit_text("Хорошо! Можешь продолжить обучение: /learn")
    await state.clear()

@router.message(UpdateStates.updating_problems)
async def on_save_problems(message: Message, state: FSMContext):
    await update_intern(message.chat.id, current_problems=message.text.strip())
    await message.answer(
        "✅ Проблемы обновлены!\n\n"
        "Теперь материалы будут ещё лучше персонализированы под твою ситуацию.\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то"
    )
    await state.clear()

@router.message(UpdateStates.updating_desires)
async def on_save_desires(message: Message, state: FSMContext):
    await update_intern(message.chat.id, desires=message.text.strip())
    await message.answer(
        "✅ Желания обновлены!\n\n"
        "Теперь материалы будут показывать, как достичь твоих целей.\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то"
    )
    await state.clear()

@router.message(UpdateStates.updating_goals)
async def on_save_goals(message: Message, state: FSMContext):
    await update_intern(message.chat.id, goals=message.text.strip())
    await message.answer(
        "✅ Цели обучения обновлены!\n\n"
        "/learn — продолжить обучение\n"
        "/update — обновить ещё что-то"
    )
    await state.clear()

@router.message(LearningStates.waiting_for_answer)
async def on_answer(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)

    if len(message.text.strip()) < 20:
        await message.answer("Напиши подробнее (хотя бы 2-3 предложения)")
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
    today = datetime.now().date()
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
        upgrade_msg = f"\n\n🎉 *Поздравляю!* Ты перешёл на уровень *{bloom['name']}*!"

    # Если уровень ниже максимального — предлагаем дополнительный вопрос
    if intern['bloom_level'] < 3:
        # Сохраняем индекс темы в state для бонусного вопроса
        await state.update_data(topic_index=intern['current_topic_index'])

        await message.answer(
            f"✅ *Тема засчитана!*\n\n"
            f"{progress_bar(done, total)}\n"
            f"{bloom['emoji']} Уровень: {bloom['name']}{upgrade_msg}\n\n"
            f"Хочешь дополнительный вопрос посложнее?",
            parse_mode="Markdown",
            reply_markup=kb_bonus_question()
        )
        # Не очищаем state — ждём выбора
    else:
        await message.answer(
            f"✅ *Тема засчитана!*\n\n"
            f"{progress_bar(done, total)}\n"
            f"{bloom['emoji']} Уровень: {bloom['name']}{upgrade_msg}\n\n"
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
    next_level = min(intern['bloom_level'] + 1, 3)
    question = await claude.generate_question(topic, intern, bloom_level=next_level)

    bloom = BLOOM_LEVELS.get(next_level, BLOOM_LEVELS[1])

    await callback.message.answer(
        f"🚀 *Бонусный вопрос* ({bloom['emoji']} {bloom['name']})\n\n"
        f"{question}\n\n"
        f"Напиши ответ 👇",
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
        await message.answer("Напиши подробнее (хотя бы 2-3 предложения)")
        return

    intern = await get_intern(message.chat.id)
    data = await state.get_data()
    topic_index = data.get('topic_index', 0)

    # Сохраняем ответ на бонусный вопрос
    await save_answer(message.chat.id, topic_index, f"[BONUS] {message.text.strip()}")

    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    await message.answer(
        f"🌟 *Отлично!* Бонусный вопрос засчитан!\n\n"
        f"Ты тренируешь навыки уровня *{bloom['name']}* и выше.\n\n"
        f"/learn — следующая тема",
        parse_mode="Markdown"
    )
    await state.clear()

# --- Отправка темы ---

async def send_topic(chat_id: int, state: FSMContext, bot: Bot):
    intern = await get_intern(chat_id)

    # Проверяем дневной лимит
    topics_today = get_topics_today(intern)
    if topics_today >= DAILY_TOPICS_LIMIT:
        await bot.send_message(
            chat_id,
            f"🎯 *Сегодня ты уже прошёл {topics_today} темы — это отлично!*\n\n"
            f"Лимит: *{DAILY_TOPICS_LIMIT} темы в день*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"*Почему так?*\n\n"
            f"Мы тренируем *систематичность* — это ключевой навык.\n\n"
            f"Намного важнее учиться *понемногу каждый день*, "
            f"чем много за раз, а потом ничего.\n\n"
            f"Регулярность > Интенсивность\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Возвращайся завтра! Или в *{intern['schedule_time']}* я сам напомню.",
            parse_mode="Markdown"
        )
        return

    topic = get_topic(intern['current_topic_index'])

    if not topic:
        await bot.send_message(chat_id, "🎉 Все темы пройдены!")
        return

    await bot.send_message(chat_id, "⏳ Генерирую персональный материал...")

    # Генерируем контент с контекстом из MCP
    content = await claude.generate_content(topic, intern, mcp_client=mcp)
    question = await claude.generate_question(topic, intern)

    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    header = (
        f"📚 *{topic['section']}* → {topic['subsection']}\n\n"
        f"*{topic['title']}*\n"
        f"⏱ {intern['study_duration']} минут\n{'─'*25}\n\n"
    )

    full = header + content
    if len(full) > 4000:
        await bot.send_message(chat_id, header, parse_mode="Markdown")
        for i in range(0, len(content), 4000):
            await bot.send_message(chat_id, content[i:i+4000])
    else:
        await bot.send_message(chat_id, full, parse_mode="Markdown")

    await bot.send_message(
        chat_id,
        f"{'─'*25}\n\n"
        f"❓ *Вопрос* ({bloom['emoji']} {bloom['name']})\n\n"
        f"{question}\n\n"
        f"⏱ 5 минут\nНапиши ответ 👇",
        parse_mode="Markdown"
    )

    await state.set_state(LearningStates.waiting_for_answer)

# ============= ПЛАНИРОВЩИК =============

scheduler = AsyncIOScheduler()

async def scheduled_check():
    """Проверка расписания каждую минуту"""
    now = datetime.now()
    interns = await get_all_scheduled_interns(now.hour, now.minute)
    
    bot = Bot(token=BOT_TOKEN)
    for intern in interns:
        try:
            await bot.send_message(
                intern['chat_id'],
                f"⏰ Время учиться, {intern['name']}!",
                reply_markup=kb_learn()
            )
            logger.info(f"Sent reminder to {intern['chat_id']}")
        except Exception as e:
            logger.error(f"Failed to send reminder to {intern['chat_id']}: {e}")
    
    await bot.session.close()

# ============= ЗАПУСК =============

async def main():
    # Инициализация БД
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Установка команд бота (Menu-кнопка)
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать обучение"),
        BotCommand(command="learn", description="Получить новую тему"),
        BotCommand(command="progress", description="Мой прогресс"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="update", description="Обновить профиль"),
        BotCommand(command="help", description="Справка")
    ])

    # Запуск планировщика
    scheduler.add_job(scheduled_check, 'cron', minute='*')
    scheduler.start()

    logger.info("🚀 Бот запущен с PostgreSQL!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
