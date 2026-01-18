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
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
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

# ============= СОСТОЯНИЯ FSM =============

class OnboardingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_role = State()
    waiting_for_domain = State()
    waiting_for_interests = State()
    waiting_for_experience = State()
    waiting_for_difficulty = State()
    waiting_for_learning_style = State()
    waiting_for_goals = State()
    waiting_for_schedule = State()
    confirming_profile = State()

class LearningStates(StatesGroup):
    waiting_for_answer = State()

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
                goals TEXT DEFAULT '',
                schedule_time TEXT DEFAULT '09:00',
                current_topic_index INTEGER DEFAULT 0,
                completed_topics TEXT DEFAULT '[]',
                onboarding_completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
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
                'goals': row['goals'],
                'schedule_time': row['schedule_time'],
                'current_topic_index': row['current_topic_index'],
                'completed_topics': json.loads(row['completed_topics']),
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
                'goals': '',
                'schedule_time': '09:00',
                'current_topic_index': 0,
                'completed_topics': [],
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

def get_personalization_prompt(intern: dict) -> str:
    """Генерирует промпт для персонализации"""
    diff = DIFFICULTY_LEVELS.get(intern['difficulty_preference'], {})
    style = LEARNING_STYLES.get(intern['learning_style'], {})
    exp = EXPERIENCE_LEVELS.get(intern['experience_level'], {})
    
    interests = ', '.join(intern['interests']) if intern['interests'] else 'не указаны'
    
    return f"""
ПРОФИЛЬ СТАЖЕРА:
- Имя: {intern['name']}
- Роль: {intern['role']}
- Предметная область: {intern['domain']}
- Интересы: {interests}
- Уровень опыта: {exp.get('name', '')} ({exp.get('desc', '')})
- Желаемая сложность: {diff.get('name', '')} ({diff.get('desc', '')})
- Стиль обучения: {style.get('name', '')} ({style.get('desc', '')})
- Цели: {intern['goals']}

ИНСТРУКЦИИ:
1. Используй примеры из области "{intern['domain']}" и интересов стажера
2. Адаптируй сложность под уровень "{diff.get('name', 'средний')}"
3. {'Начинай с теории' if intern['learning_style'] == 'theoretical' else 'Начинай с практических примеров' if intern['learning_style'] == 'practical' else 'Чередуй теорию и практику'}
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

    async def generate_content(self, topic: dict, intern: dict) -> str:
        system_prompt = f"""Ты — персональный наставник.
{get_personalization_prompt(intern)}

Создай текст на 20 минут чтения (~2000 слов). Без заголовков, только абзацы."""

        user_prompt = f"""Тема: {topic.get('title')}
Основное понятие: {topic.get('main_concept')}
Связанные понятия: {', '.join(topic.get('related_concepts', []))}"""

        result = await self.generate(system_prompt, user_prompt)
        return result or "Не удалось сгенерировать контент. Попробуйте /learn ещё раз."

    async def generate_question(self, topic: dict, intern: dict) -> str:
        system_prompt = f"""Создай один вопрос для проверки понимания темы.
{get_personalization_prompt(intern)}
Вопрос должен требовать развёрнутого ответа и быть связан с областью стажера."""

        user_prompt = f"""Тема: {topic.get('title')}
Понятие: {topic.get('main_concept')}"""

        result = await self.generate(system_prompt, user_prompt)
        return result or "Что ты понял из этой темы? Приведи пример из своей практики."

claude = ClaudeClient()

# ============= ТЕМЫ =============

TOPICS = [
    {
        "id": "what-is-system",
        "section": "Системное мышление",
        "subsection": "Основы",
        "title": "Что такое система",
        "main_concept": "система",
        "related_concepts": ["элементы", "связи", "эмерджентность"]
    },
    {
        "id": "system-approach",
        "section": "Системное мышление",
        "subsection": "Основы",
        "title": "Системный подход",
        "main_concept": "системный подход",
        "related_concepts": ["редукционизм", "холизм", "анализ"]
    },
    {
        "id": "system-boundaries",
        "section": "Системное мышление",
        "subsection": "Основы",
        "title": "Границы системы",
        "main_concept": "границы системы",
        "related_concepts": ["окружение", "интерфейс", "контекст"]
    }
]

def get_topic(index: int) -> Optional[dict]:
    return TOPICS[index] if index < len(TOPICS) else None

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
    
    await message.answer(
        f"📋 *Твой профиль:*\n\n"
        f"👤 {intern['name']}\n"
        f"💼 {intern['role']}\n"
        f"🎯 {intern['domain']}\n"
        f"🎨 {', '.join(intern['interests'])}\n\n"
        f"{exp.get('emoji','')} {exp.get('name','')}\n"
        f"{diff.get('emoji','')} {diff.get('name','')}\n"
        f"{style.get('emoji','')} {style.get('name','')}\n\n"
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
    
    await callback.answer("Сохранено!")
    await callback.message.edit_text(
        f"✅ *Готово!*\n\n"
        f"Буду отправлять материал в *{intern['schedule_time']}*\n\n"
        f"• 20 мин — изучение\n"
        f"• 5 мин — ответ на вопрос\n"
        f"• Ответил = тема засчитана ✅\n\n"
        f"Начнём?",
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
    total = len(TOPICS)
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
    
    await message.answer(
        f"👤 *{intern['name']}*\n"
        f"💼 {intern['role']}\n"
        f"🎯 {intern['domain']}\n"
        f"🎨 {', '.join(intern['interests'])}\n\n"
        f"{exp.get('emoji','')} {exp.get('name','')}\n"
        f"{diff.get('emoji','')} {diff.get('name','')}\n"
        f"{style.get('emoji','')} {style.get('name','')}\n\n"
        f"⏰ Обучение в {intern['schedule_time']}",
        parse_mode="Markdown"
    )

@router.message(LearningStates.waiting_for_answer)
async def on_answer(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    
    if len(message.text.strip()) < 20:
        await message.answer("Напиши подробнее (хотя бы 2-3 предложения)")
        return
    
    # Сохраняем ответ
    await save_answer(message.chat.id, intern['current_topic_index'], message.text.strip())
    
    # Обновляем прогресс
    completed = intern['completed_topics'] + [intern['current_topic_index']]
    await update_intern(
        message.chat.id,
        completed_topics=completed,
        current_topic_index=intern['current_topic_index'] + 1
    )
    
    done = len(completed)
    total = len(TOPICS)
    
    await message.answer(
        f"✅ *Тема засчитана!*\n\n"
        f"{progress_bar(done, total)}\n\n"
        f"/learn — следующая тема",
        parse_mode="Markdown"
    )
    await state.clear()

# --- Отправка темы ---

async def send_topic(chat_id: int, state: FSMContext, bot: Bot):
    intern = await get_intern(chat_id)
    topic = get_topic(intern['current_topic_index'])
    
    if not topic:
        await bot.send_message(chat_id, "🎉 Все темы пройдены!")
        return
    
    await bot.send_message(chat_id, "⏳ Генерирую персональный материал...")
    
    content = await claude.generate_content(topic, intern)
    question = await claude.generate_question(topic, intern)
    
    header = (
        f"📚 *{topic['section']}* → {topic['subsection']}\n\n"
        f"*{topic['title']}*\n"
        f"⏱ 20 минут\n{'─'*25}\n\n"
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
        f"{'─'*25}\n\n❓ *Вопрос:*\n\n{question}\n\n⏱ 5 минут\nНапиши ответ 👇",
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
    
    # Запуск планировщика
    scheduler.add_job(scheduled_check, 'cron', minute='*')
    scheduler.start()
    
    logger.info("🚀 Бот запущен с PostgreSQL!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
