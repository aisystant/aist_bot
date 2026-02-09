"""
Хендлеры прогресса (/progress, full report, progress_back).
"""

import logging

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command

from config import MARATHON_DAYS
from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

progress_router = Router(name="progress")


def _bot_imports():
    """Lazy imports to avoid circular imports."""
    from core.topics import (
        get_marathon_day, get_lessons_tasks_progress,
        get_days_progress, TOPICS,
    )
    return {
        'get_marathon_day': get_marathon_day,
        'get_lessons_tasks_progress': get_lessons_tasks_progress,
        'get_days_progress': get_days_progress,
        'TOPICS': TOPICS,
    }


@progress_router.message(Command("progress"))
async def cmd_progress(message: Message):
    """Короткий отчёт прогресса за текущую неделю"""
    from db.queries.answers import get_weekly_marathon_stats, get_weekly_feed_stats
    from db.queries.activity import get_activity_stats
    b = _bot_imports()

    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('progress.first_start', lang))
        return

    chat_id = message.chat.id
    lang = intern.get('language', 'ru') or 'ru'

    try:
        activity_stats = await get_activity_stats(chat_id)
        marathon_stats = await get_weekly_marathon_stats(chat_id)
        feed_stats = await get_weekly_feed_stats(chat_id)
    except Exception as e:
        logger.error(f"Ошибка получения статистики для {chat_id}: {e}")
        activity_stats = {'days_active_this_week': 0}
        marathon_stats = {'work_products': 0}
        feed_stats = {'digests': 0, 'fixations': 0}

    days_active_week = activity_stats.get('days_active_this_week', 0)

    marathon_day = b['get_marathon_day'](intern)
    lessons_week = marathon_stats.get('theory_answers', 0)
    tasks_week = marathon_stats.get('work_products', 0)

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

    text += f"🏃 *{t('progress.marathon_title', lang)}*\n"
    text += f"{t('progress.day_of_total', lang, day=marathon_day, total=MARATHON_DAYS)}\n"
    text += f"📖 {t('progress.lessons', lang)}: {lessons_week}. 📝 {t('progress.tasks', lang)}: {tasks_week}\n\n"

    text += f"📚 *{t('progress.feed_title', lang)}*\n"
    text += f"{t('progress.digests', lang)}: {feed_stats.get('digests', 0)}. {t('progress.fixations', lang)}: {feed_stats.get('fixations', 0)}\n"
    text += f"{t('progress.topics', lang)}: {feed_topics_text}"

    from config import Mode
    current_mode = intern.get('mode', Mode.MARATHON)

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


@progress_router.callback_query(F.data == "progress_full")
async def show_full_progress(callback: CallbackQuery):
    """Полный отчёт с начала использования бота"""
    await callback.answer()
    b = _bot_imports()

    try:
        from db.queries.answers import get_total_stats, get_work_products_by_day

        chat_id = callback.message.chat.id
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

        if not intern:
            await callback.message.edit_text(t('profile.not_found', lang))
            return

        try:
            total_stats = await get_total_stats(chat_id)
        except Exception as e:
            logger.error(f"Ошибка получения total_stats: {e}")
            total_stats = {}

        reg_date = total_stats.get('registered_at')
        if reg_date:
            date_str = reg_date.strftime('%d.%m.%Y')
        else:
            date_str = "—"

        days_since = total_stats.get('days_since_start', 1)
        total_active = total_stats.get('total_active_days', 0)

        marathon_day = b['get_marathon_day'](intern)
        progress = b['get_lessons_tasks_progress'](intern.get('completed_topics', []))

        try:
            wp_by_day = await get_work_products_by_day(chat_id, b['TOPICS'])
        except Exception as e:
            logger.error(f"Ошибка получения wp_by_day: {e}")
            wp_by_day = {}

        days_progress = b['get_days_progress'](intern.get('completed_topics', []), marathon_day)

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

            lesson_text = f"{t('progress.lesson_short', lang)}: {d['lessons_completed']}"
            task_text = f"{t('progress.task_short', lang)}: {d['tasks_completed']}"
            wp_text = f"{t('progress.wp_short', lang)}: {wp_count}"
            days_text += f"   {emoji} {t('progress.day_text', lang, day=day_num)}: {lesson_text} | {task_text} | {wp_text}\n"

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

        text += f"🏃 *{t('progress.marathon_title', lang)}*\n"
        text += f"{t('progress.day', lang, day=marathon_day, total=MARATHON_DAYS)}\n"
        text += f"📖 {t('progress.lessons', lang)}: {progress['lessons']['completed']}/{progress['lessons']['total']}\n"
        text += f"📝 {t('progress.tasks', lang)}: {progress['tasks']['completed']}/{progress['tasks']['total']}\n"
        text += f"{t('progress.work_products_count', lang)}: {total_stats.get('total_work_products', 0)}\n"

        if days_text:
            text += f"\n📋 *{t('progress.by_days', lang)}:*\n{days_text}"

        days_progress = b['get_days_progress'](intern.get('completed_topics', []), marathon_day)
        completed_days = sum(1 for d in days_progress if d['status'] == 'completed')
        lag = marathon_day - completed_days
        text += f"{t('progress.lag', lang)}: {lag} {t('progress.days', lang)}\n"

        text += f"\n📚 *{t('progress.feed_title', lang)}*\n"
        text += f"{t('progress.digests_count', lang)}: {total_stats.get('total_digests', 0)}\n"
        text += f"{t('progress.fixations_count', lang)}: {total_stats.get('total_fixations', 0)}\n"
        text += f"{t('progress.topics_colon', lang)}: {feed_topics_text}"

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
        try:
            intern = await get_intern(callback.message.chat.id)
            lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
        except Exception:
            lang = 'ru'
        await callback.message.edit_text(
            f"{t('progress.full_report_error', lang)}\n\n/progress"
        )


@progress_router.callback_query(F.data == "progress_back")
async def progress_back(callback: CallbackQuery):
    """Возврат к короткому отчёту"""
    await callback.answer()

    try:
        await callback.message.delete()
        await callback.message.answer(
            "Для обновлённого отчёта используйте /progress"
        )
    except Exception as e:
        logger.error(f"Ошибка в progress_back: {e}")
        await callback.message.edit_text(
            "/progress — посмотреть прогресс"
        )
