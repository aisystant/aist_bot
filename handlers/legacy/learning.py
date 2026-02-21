"""
Legacy learning хендлеры — обработка ответов на теорию, практику, бонусы.

Используются при USE_STATE_MACHINE=false.
При включённом SM делают bypass в State Machine.

Перенесено из bot.py для уменьшения размера монолита.
"""

import asyncio
import logging
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db.queries import get_intern, update_intern, get_topics_today
from db.queries.answers import save_answer
from db.queries.users import moscow_today
from config import MARATHON_DAYS, MAX_TOPICS_PER_DAY
from i18n import t
from engines.shared import handle_question, ProcessingStage
from integrations.telegram.keyboards import (
    kb_bonus_question, kb_skip_topic, kb_submit_work_product, progress_bar,
)
from clients.mcp import mcp_knowledge
from clients.claude import ClaudeClient
from helpers.message_split import prepare_html_parts

logger = logging.getLogger(__name__)

legacy_learning_router = Router(name="legacy_learning")


# ============= СОСТОЯНИЯ FSM =============

class LearningStates(StatesGroup):
    waiting_for_answer = State()           # ответ на вопрос теории
    waiting_for_work_product = State()     # название рабочего продукта (практика)
    waiting_for_bonus_answer = State()     # ответ на дополнительный вопрос посложнее


# ============= LAZY IMPORTS =============
# Функции из bot.py, которые ещё не перенесены в shared модули

def _bot_imports():
    """Lazy imports to avoid circular imports."""
    from core.topics import (
        get_marathon_day, get_topic, get_total_topics, get_topic_title,
        get_available_topics, get_next_topic_index, get_topics_for_day,
        get_practice_for_day, get_lessons_tasks_progress, TOPICS,
    )
    from config import BLOOM_AUTO_UPGRADE_AFTER
    from bot import claude, state_machine
    return {
        'get_marathon_day': get_marathon_day,
        'get_topic': get_topic,
        'get_total_topics': get_total_topics,
        'get_topic_title': get_topic_title,
        'get_available_topics': get_available_topics,
        'get_next_topic_index': get_next_topic_index,
        'get_topics_for_day': get_topics_for_day,
        'get_practice_for_day': get_practice_for_day,
        'get_lessons_tasks_progress': get_lessons_tasks_progress,
        'TOPICS': TOPICS,
        'BLOOM_AUTO_UPGRADE_AFTER': BLOOM_AUTO_UPGRADE_AFTER,
        'claude': claude,
        'state_machine': state_machine,
    }


# ============= ХЕНДЛЕРЫ =============

@legacy_learning_router.message(LearningStates.waiting_for_answer)
async def on_answer(message: Message, state: FSMContext, bot=None):
    b = _bot_imports()
    chat_id = message.chat.id
    text = message.text or ''
    current_state = await state.get_state()
    logger.info(f"[on_answer] ВЫЗВАН для chat_id={chat_id}, state={current_state}, text={text[:50] if text else '[no text]'}")
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    # Bypass: если State Machine включён и у пользователя есть SM-состояние
    if b['state_machine'] is not None and intern and intern.get('current_state'):
        logger.info(f"[on_answer] Bypassing legacy handler, SM state: {intern.get('current_state')}")
        await state.clear()
        await b['state_machine'].handle(intern, message)
        return

    # Проверяем, это вопрос к ИИ (начинается с ?)
    if text.strip().startswith('?'):
        question_text = text.strip()[1:].strip()
        if question_text:
            progress_msg = await message.answer(t('loading.progress.analyzing', lang))
            try:
                answer, sources = await handle_question(
                    question=question_text,
                    intern=intern,
                    context_topic=b['get_topic'](intern['current_topic_index']),
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
            final_state = await state.get_state()
            logger.info(f"[on_answer] После обработки вопроса, state={final_state} для chat_id={chat_id}")
            return

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
    if topics_at_bloom >= b['BLOOM_AUTO_UPGRADE_AFTER'] and bloom_level < 3:
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
    total = b['get_total_topics']()
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
    next_available = b['get_available_topics'](updated_intern)
    next_topic_hint = ""
    next_command = t('marathon.next_command', lang)
    if next_available:
        next_topic = next_available[0][1]
        if next_topic.get('type') == 'practice':
            next_topic_hint = f"\n\n📝 *{t('marathon.next_task', lang)}:* {b['get_topic_title'](next_topic, lang)}"
            next_command = t('marathon.continue_to_task', lang)
        else:
            next_topic_hint = f"\n\n📚 *{t('marathon.next_lesson', lang)}:* {b['get_topic_title'](next_topic, lang)}"
            next_command = t('marathon.continue_to_lesson', lang)

    # Если уровень ниже максимального — предлагаем дополнительный вопрос
    if intern['bloom_level'] < 3:
        await state.update_data(topic_index=intern['current_topic_index'], next_command=next_command)

        await message.answer(
            f"✅ *{t('marathon.topic_completed', lang)}*\n\n"
            f"{progress_bar(done, total)}\n"
            f"{t(f'bloom.level_{bloom_level}_short', lang)}{upgrade_msg}{next_topic_hint}\n\n"
            f"{t('marathon.want_harder', lang)}",
            parse_mode="Markdown",
            reply_markup=kb_bonus_question(lang)
        )
    else:
        # Уровень максимальный, бонус не предлагаем
        completed_topic = b['TOPICS'][intern['current_topic_index']]
        practice = b['get_practice_for_day'](updated_intern, completed_topic['day'])

        if practice:
            practice_index, practice_topic = practice
            await message.answer(
                f"✅ *{t('marathon.topic_completed', lang)}*\n\n"
                f"{progress_bar(done, total)}\n"
                f"{t(f'bloom.level_{bloom_level}_short', lang)}{upgrade_msg}\n\n"
                f"⏳ {t('marathon.loading_practice', lang)}",
                parse_mode="Markdown"
            )
            await update_intern(chat_id, current_topic_index=practice_index)
            _bot = bot or message.bot
            await send_practice_topic(chat_id, practice_topic, updated_intern, state, _bot)
        else:
            await message.answer(
                f"✅ *{t('marathon.topic_completed', lang)}*\n\n"
                f"{progress_bar(done, total)}\n"
                f"{t(f'bloom.level_{bloom_level}_short', lang)}{upgrade_msg}\n\n"
                f"✅ {t('marathon.day_complete', lang)}",
                parse_mode="Markdown"
            )
            await state.clear()

@legacy_learning_router.callback_query(F.data == "bonus_yes")
async def on_bonus_yes(callback: CallbackQuery, state: FSMContext):
    b = _bot_imports()
    await callback.answer()
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    logger.info(f"[BONUS] on_bonus_yes вызван для chat_id={chat_id}, user_id={user_id}")

    data = await state.get_data()
    topic_index = data.get('topic_index', 0)
    next_command = data.get('next_command')
    logger.info(f"[BONUS] State data: topic_index={topic_index}, next_command={next_command}")

    intern = await get_intern(chat_id)
    topic = b['get_topic'](topic_index)
    lang = intern.get('language', 'ru') if intern else 'ru'

    if not topic:
        await callback.message.edit_text(f"Не удалось найти тему.\n\n{next_command or t('marathon.next_command', lang)}")
        await state.clear()
        return

    await callback.message.edit_text(f"⏳ {t('marathon.generating_harder', lang)}")

    try:
        marathon_day = b['get_marathon_day'](intern)
        next_level = min(intern['bloom_level'] + 1, 3)
        logger.info(f"[BONUS] Генерируем вопрос уровня {next_level} для темы {topic_index}")
        question = await b['claude'].generate_question(topic, intern, bloom_level=next_level)

        await state.update_data(topic_index=topic_index, next_command=next_command, bonus_level=next_level)
        await state.set_state(LearningStates.waiting_for_bonus_answer)
        current_state = await state.get_state()
        logger.info(f"[BONUS] Состояние установлено ДО отправки сообщения: {current_state}")

        await callback.message.answer(
            f"🚀 *{t('marathon.bonus_question', lang)}* ({t(f'bloom.level_{next_level}_short', lang)})\n\n"
            f"{question}\n\n"
            f"{t('marathon.write_answer', lang)}",
            parse_mode="Markdown"
        )

        final_state = await state.get_state()
        logger.info(f"[BONUS] Состояние после отправки сообщения: {final_state}")
    except Exception as e:
        logger.error(f"Ошибка генерации бонусного вопроса: {e}")
        import traceback
        logger.error(f"[BONUS] Traceback: {traceback.format_exc()}")
        await callback.message.answer(
            f"Не удалось сгенерировать бонусный вопрос. Попробуйте позже.\n\n"
            f"{next_command or t('marathon.next_command', lang)}"
        )
        await state.clear()

@legacy_learning_router.callback_query(F.data == "bonus_no")
async def on_bonus_no(callback: CallbackQuery, state: FSMContext, bot=None):
    b = _bot_imports()
    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    data = await state.get_data()
    next_command = data.get('next_command', t('marathon.next_command', lang))
    await callback.answer(t('marathon.ok', lang))

    topic_index = data.get('topic_index', 0)
    completed_topic = b['TOPICS'][topic_index] if topic_index < len(b['TOPICS']) else None
    practice = b['get_practice_for_day'](intern, completed_topic['day']) if completed_topic else None

    if practice:
        practice_index, practice_topic = practice
        await callback.message.edit_text(
            callback.message.text + f"\n\n⏳ {t('marathon.loading_practice', lang)}",
            parse_mode="Markdown"
        )
        await update_intern(chat_id, current_topic_index=practice_index)
        _bot = bot or callback.bot
        await send_practice_topic(chat_id, practice_topic, intern, state, _bot)
    else:
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ {t('marathon.day_complete', lang)}",
            parse_mode="Markdown"
        )
        await state.clear()

@legacy_learning_router.message(LearningStates.waiting_for_bonus_answer)
async def on_bonus_answer(message: Message, state: FSMContext, bot=None):
    b = _bot_imports()
    chat_id = message.chat.id
    text = message.text or ''
    current_state = await state.get_state()
    logger.info(f"[BONUS] on_bonus_answer вызван для chat_id={chat_id}, state={current_state}")

    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    # Bypass: если State Machine включён и у пользователя есть SM-состояние
    if b['state_machine'] is not None and intern and intern.get('current_state'):
        logger.info(f"[on_bonus_answer] Bypassing legacy handler, SM state: {intern.get('current_state')}")
        await state.clear()
        await b['state_machine'].handle(intern, message)
        return

    # Проверяем, это вопрос к ИИ (начинается с ?)
    if text.strip().startswith('?'):
        question_text = text.strip()[1:].strip()
        if question_text:
            data = await state.get_data()
            topic_index = data.get('topic_index', 0)
            progress_msg = await message.answer(t('loading.progress.analyzing', lang))
            try:
                answer, sources = await handle_question(
                    question=question_text,
                    intern=intern,
                    context_topic=b['get_topic'](topic_index),
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
            return

    if len(text.strip()) < 20:
        await message.answer(t('marathon.write_more_details', lang))
        return

    data = await state.get_data()
    topic_index = data.get('topic_index', 0)
    logger.info(f"[BONUS] Processing answer: topic_index={topic_index}, data_keys={list(data.keys())}")

    try:
        await save_answer(chat_id, topic_index, f"[BONUS] {text.strip()}")

        bloom_level = intern['bloom_level'] if intern else 1

        completed_topic = b['TOPICS'][topic_index] if topic_index < len(b['TOPICS']) else None
        practice = b['get_practice_for_day'](intern, completed_topic['day']) if completed_topic else None

        if practice:
            practice_index, practice_topic = practice
            await message.answer(
                f"🌟 *{t('marathon.bonus_completed', lang)}*\n\n"
                f"{t('marathon.training_skills', lang)} *{t(f'bloom.level_{bloom_level}_short', lang)}* {t('marathon.and_higher', lang)}\n\n"
                f"⏳ {t('marathon.loading_practice', lang)}",
                parse_mode="Markdown"
            )
            await update_intern(chat_id, current_topic_index=practice_index)
            _bot = bot or message.bot
            await send_practice_topic(chat_id, practice_topic, intern, state, _bot)
        else:
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

@legacy_learning_router.callback_query(LearningStates.waiting_for_answer, F.data == "skip_topic")
async def on_skip_topic(callback: CallbackQuery, state: FSMContext):
    b = _bot_imports()
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    next_index = intern['current_topic_index'] + 1
    await update_intern(callback.message.chat.id, current_topic_index=next_index)

    topic = b['get_topic'](intern['current_topic_index'])
    topic_title = b['get_topic_title'](topic, lang) if topic else t('marathon.topic_default', lang)

    await callback.answer(t('marathon.topic_skipped', lang))
    await callback.message.edit_text(
        t('marathon.topic_skipped_message', lang, title=topic_title),
        parse_mode="Markdown"
    )
    await state.clear()


@legacy_learning_router.message(LearningStates.waiting_for_work_product)
async def on_work_product(message: Message, state: FSMContext):
    b = _bot_imports()
    text = message.text or ''
    chat_id = message.chat.id
    current_state = await state.get_state()
    logger.info(f"[on_work_product] ВЫЗВАН для chat_id={chat_id}, state={current_state}, text={text[:50] if text else '[no text]'}")
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    # Bypass: если State Machine включён и у пользователя есть SM-состояние
    if b['state_machine'] is not None and intern and intern.get('current_state'):
        logger.info(f"[on_work_product] Bypassing legacy handler, SM state: {intern.get('current_state')}")
        await state.clear()
        await b['state_machine'].handle(intern, message)
        return

    # Проверяем, это вопрос к ИИ (начинается с ?)
    if text.strip().startswith('?'):
        question_text = text.strip()[1:].strip()
        if question_text:
            progress_msg = await message.answer(t('loading.progress.analyzing', lang))
            try:
                answer, sources = await handle_question(
                    question=question_text,
                    intern=intern,
                    context_topic=b['get_topic'](intern['current_topic_index']),
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
            return

    if len(text.strip()) < 3:
        await message.answer(f"{t('marathon.write_wp_minimum', lang)} ({t('marathon.wp_example_hint', lang)})")
        return

    topic_index = intern['current_topic_index']
    await save_answer(
        message.chat.id,
        topic_index,
        f"[РП] {text.strip()}"
    )

    topic = b['get_topic'](topic_index)
    topic_day = topic['day'] if topic else b['get_marathon_day'](intern)

    completed = intern['completed_topics'] + [topic_index]

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
    total = b['get_total_topics']()

    day_topics = b['get_topics_for_day'](topic_day)
    day_completed = sum(1 for i, _ in enumerate(b['TOPICS']) if b['TOPICS'][i]['day'] == topic_day and i in completed)

    if day_completed >= len(day_topics):
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


@legacy_learning_router.callback_query(LearningStates.waiting_for_work_product, F.data == "skip_practice")
async def on_skip_practice(callback: CallbackQuery, state: FSMContext):
    b = _bot_imports()
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    next_index = intern['current_topic_index'] + 1
    await update_intern(callback.message.chat.id, current_topic_index=next_index)

    topic = b['get_topic'](intern['current_topic_index'])
    topic_title = b['get_topic_title'](topic, lang) if topic else t('marathon.practice_default', lang)

    await callback.answer(t('marathon.practice_skipped', lang))
    await callback.message.edit_text(
        t('marathon.practice_skipped_message', lang, title=topic_title),
        parse_mode="Markdown"
    )
    await state.clear()


# ============= ОТПРАВКА ТЕМ =============

async def send_topic(chat_id: int, state: Optional[FSMContext], bot):
    """Отправка следующей темы (legacy entry point)."""
    b = _bot_imports()
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    marathon_day = b['get_marathon_day'](intern)

    # Автоматический запуск марафона при первом /learn
    if marathon_day == 0:
        start_date = intern.get('marathon_start_date')
        if start_date:
            await bot.send_message(
                chat_id,
                f"🗓 {t('marathon.marathon_not_started', lang)}\n\n"
                f"{t('marathon.marathon_starts', lang, date=start_date.strftime('%d.%m.%Y'))}\n\n"
                f"{t('update.update_more', lang)}",
                parse_mode="Markdown"
            )
            return
        else:
            today = moscow_today()
            await update_intern(chat_id, marathon_start_date=today)
            await bot.send_message(
                chat_id,
                f"🚀 *{t('marathon.marathon_launched', lang)}*\n\n"
                f"{t('marathon.marathon_starts', lang, date=today.strftime('%d.%m.%Y'))} ({t('update.today', lang)})\n\n"
                f"{t('update.update_more', lang)}",
                parse_mode="Markdown"
            )
            intern = await get_intern(chat_id)
            marathon_day = b['get_marathon_day'](intern)

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

    topic_index = b['get_next_topic_index'](intern)
    topic = b['get_topic'](topic_index) if topic_index is not None else None

    if topic_index is not None and topic_index != intern['current_topic_index']:
        await update_intern(chat_id, current_topic_index=topic_index)

    if not topic:
        total_topics = b['get_total_topics']()
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

        available = b['get_available_topics'](intern)
        if not available and completed_count < total_topics:
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
            progress = b['get_lessons_tasks_progress'](intern['completed_topics'])

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

    topic_type = topic.get('type', 'theory')

    if topic_type == 'theory':
        await send_theory_topic(chat_id, topic, intern, state, bot)
    else:
        await send_practice_topic(chat_id, topic, intern, state, bot)


async def send_theory_topic(chat_id: int, topic: dict, intern: dict, state: Optional[FSMContext], bot):
    """Отправка теоретической темы."""
    b = _bot_imports()
    marathon_day = b['get_marathon_day'](intern)
    topic_day = topic.get('day', marathon_day)
    lang = intern.get('language', 'ru')
    bloom_level = intern['bloom_level']

    await bot.send_chat_action(chat_id=chat_id, action="typing")
    await bot.send_message(chat_id, f"⏳ {t('marathon.generating_material', lang)}")

    content = None
    try:
        content = await asyncio.wait_for(
            b['claude'].generate_content(topic, intern, mcp_client=mcp_knowledge),
            timeout=60.0
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

    question = None
    try:
        question = await asyncio.wait_for(
            b['claude'].generate_question(topic, intern),
            timeout=30.0
        )
    except asyncio.TimeoutError:
        logger.warning(f"[send_theory_topic] Таймаут генерации вопроса для chat_id={chat_id}")
    except Exception as e:
        logger.error(f"[send_theory_topic] Ошибка генерации вопроса: {e}")

    if not question:
        question = t('marathon.fallback_question', lang, topic=topic.get('title', 'тема'))

    header = (
        f"📚 *{t('marathon.day_theory', lang, day=topic_day)}*\n"
        f"*{b['get_topic_title'](topic, lang)}*\n"
        f"⏱ {t('marathon.minutes', lang, minutes=intern['study_duration'])}\n\n"
    )

    full = header + content
    parts = prepare_html_parts(full)
    for part in parts:
        await bot.send_message(chat_id, part, parse_mode="HTML")

    if state:
        await state.set_state(LearningStates.waiting_for_answer)

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


async def send_practice_topic(chat_id: int, topic: dict, intern: dict, state: Optional[FSMContext], bot):
    """Отправка практической темы."""
    b = _bot_imports()
    marathon_day = b['get_marathon_day'](intern)
    topic_day = topic.get('day', marathon_day)
    lang = intern.get('language', 'ru')

    await bot.send_chat_action(chat_id=chat_id, action="typing")
    await bot.send_message(chat_id, f"⏳ {t('marathon.preparing_practice', lang)}")

    intro = ""
    task = topic.get('task', '')
    work_product = topic.get('work_product', '')
    examples = topic.get('work_product_examples', [])

    try:
        practice_data = await asyncio.wait_for(
            b['claude'].generate_practice_intro(topic, intern),
            timeout=30.0
        )
        if isinstance(practice_data, dict):
            intro = practice_data.get('intro', '')
            task = practice_data.get('task', '') or task
            work_product = practice_data.get('work_product', '') or work_product
        elif isinstance(practice_data, str):
            intro = practice_data
    except asyncio.TimeoutError:
        logger.warning(f"[send_practice_topic] Таймаут генерации intro для chat_id={chat_id}")
    except Exception as e:
        logger.error(f"[send_practice_topic] Ошибка генерации intro: {e}")

    examples_text = ""
    if examples:
        examples_text = f"\n*{t('marathon.wp_examples', lang)}:*\n" + "\n".join([f"• {ex}" for ex in examples])

    header = (
        f"✏️ *{t('marathon.day_practice', lang, day=topic_day)}*\n"
        f"*{b['get_topic_title'](topic, lang)}*\n\n"
    )

    content = f"{intro}\n\n" if intro else ""
    content += f"📋 *{t('marathon.task', lang)}:*\n{task}\n\n"
    content += f"🎯 *{t('marathon.work_product', lang)}:* {work_product}"
    content += examples_text

    full = header + content
    parts = prepare_html_parts(full)
    for part in parts:
        await bot.send_message(chat_id, part, parse_mode="HTML")

    if state:
        await state.set_state(LearningStates.waiting_for_work_product)

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
