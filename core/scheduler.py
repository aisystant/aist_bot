"""
Планировщик — отправка тем по расписанию и напоминания.

Извлечён из bot.py. Использует core.dispatcher для SM-роутинга.
"""

import logging
import os
from datetime import timedelta
from typing import Optional

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import MOSCOW_TZ, MAX_TOPICS_PER_DAY, MARATHON_DAYS
from db.queries import get_intern, update_intern, get_all_scheduled_interns, get_topics_today
from db.queries.users import moscow_now
from i18n import t

logger = logging.getLogger(__name__)

# --- Module state ---
_scheduler: Optional[AsyncIOScheduler] = None
_aiogram_dispatcher = None  # aiogram Dispatcher (for FSM storage access)
_bot_dispatcher = None      # core.dispatcher.Dispatcher (for SM routing)
_bot_token: str = None


def init_scheduler(bot_dispatcher, aiogram_dispatcher, bot_token: str) -> AsyncIOScheduler:
    """Инициализировать и вернуть планировщик.

    Args:
        bot_dispatcher: core.dispatcher.Dispatcher (SM routing)
        aiogram_dispatcher: aiogram Dispatcher (FSM storage)
        bot_token: Telegram bot token
    """
    global _scheduler, _bot_dispatcher, _aiogram_dispatcher, _bot_token
    _bot_dispatcher = bot_dispatcher
    _aiogram_dispatcher = aiogram_dispatcher
    _bot_token = bot_token

    _scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    _scheduler.add_job(scheduled_check, 'cron', minute='*')
    _scheduler.start()

    logger.info("[Scheduler] Планировщик инициализирован")
    return _scheduler


async def send_feed_notification(chat_id: int, bot: Bot):
    """Отправка уведомления о готовности дайджеста для пользователей в режиме Лента."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    intern = await get_intern(chat_id)
    if not intern:
        return

    lang = intern.get('language', 'ru') or 'ru'
    feed_status = intern.get('feed_status', 'not_started')

    if feed_status != 'active':
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📖 {t('buttons.get_digest', lang)}", callback_data="feed_get_digest")]
    ])

    try:
        await bot.send_message(
            chat_id,
            f"*{t('reminders.feed_digest_reminder', lang)}*\n\n"
            f"{t('reminders.feed_digest_cta', lang)}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        logger.info(f"[Scheduler] Sent feed notification to {chat_id}")
    except Exception as e:
        error_msg = str(e).lower()
        if 'blocked' not in error_msg and 'deactivated' not in error_msg:
            logger.error(f"[Scheduler] Error sending feed notification to {chat_id}: {e}")


async def send_scheduled_topic(chat_id: int, bot: Bot):
    """Отправка темы марафона по расписанию."""
    from core.topics import get_marathon_day, get_next_topic_index, get_topic, get_total_topics, get_lessons_tasks_progress

    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    # Проверяем что марафон активен
    marathon_status = intern.get('marathon_status', 'not_started')
    if marathon_status != 'active':
        logger.info(f"[Scheduler] {chat_id}: marathon not active ({marathon_status}), skip")
        return

    marathon_day = get_marathon_day(intern)

    # Проверяем, начался ли марафон
    if marathon_day == 0:
        logger.info(f"[Scheduler] {chat_id}: marathon_day=0, пропуск (марафон не начался)")
        return

    # Проверяем дневной лимит
    topics_today = get_topics_today(intern)
    if topics_today >= MAX_TOPICS_PER_DAY:
        logger.info(f"[Scheduler] {chat_id}: topics_today={topics_today}, пропуск (лимит)")
        return

    # Получаем следующую тему
    topic_index = get_next_topic_index(intern)
    topic = get_topic(topic_index) if topic_index is not None else None

    if not topic:
        # Проверяем, все ли темы пройдены
        total = get_total_topics()
        completed_count = len(intern['completed_topics'])
        if completed_count >= total:
            # Марафон пройден
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
    if _bot_dispatcher and _bot_dispatcher.is_sm_active:
        logger.info(f"[Scheduler] Routing to StateMachine: chat_id={chat_id}, topic_type={topic_type}")
        try:
            # Определяем целевой стейт
            if topic_type == 'theory':
                target_state = "workshop.marathon.lesson"
            else:
                target_state = "workshop.marathon.task"

            # Очищаем legacy FSM state перед SM routing
            if _aiogram_dispatcher:
                fsm_ctx = FSMContext(
                    storage=_aiogram_dispatcher.storage,
                    key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id)
                )
                await fsm_ctx.clear()

            # Обновляем intern с актуальным topic_index перед переходом
            updated_intern = {**intern, 'current_topic_index': topic_index}

            await _bot_dispatcher.sm.go_to(updated_intern, target_state, context={'topic_index': topic_index, 'from_scheduler': True})
            logger.info(f"[Scheduler] Successfully routed to {target_state} for chat_id={chat_id}")
            return
        except Exception as e:
            logger.error(f"[Scheduler] Error routing to StateMachine: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Fallback на legacy функции

    # === Legacy routing ===
    if _aiogram_dispatcher:
        from handlers.legacy.learning import send_theory_topic, send_practice_topic

        state = FSMContext(
            storage=_aiogram_dispatcher.storage,
            key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id)
        )

        if topic_type == 'theory':
            await send_theory_topic(chat_id, topic, intern, state, bot)
        else:
            await send_practice_topic(chat_id, topic, intern, state, bot)


async def schedule_reminders(chat_id: int, intern: dict):
    """Планирует напоминания для пользователя."""
    from db import get_pool

    now = moscow_now()

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
    """Отправляет напоминание."""
    from core.topics import get_marathon_day

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
    """Проверяет и отправляет запланированные напоминания."""
    from db import get_pool

    now = moscow_now()
    now_naive = now.replace(tzinfo=None)

    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(
            '''SELECT id, chat_id, reminder_type FROM reminders
               WHERE sent = FALSE AND scheduled_for <= $1''',
            now_naive
        )

        if not rows:
            return

        bot = Bot(token=_bot_token)

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
                if 'blocked' in error_msg or 'deactivated' in error_msg or 'chat not found' in error_msg:
                    logger.warning(f"User {row['chat_id']} blocked bot, marking reminder {row['id']} as sent")
                    await conn.execute(
                        'UPDATE reminders SET sent = TRUE WHERE id = $1',
                        row['id']
                    )
                else:
                    logger.error(f"Failed to send reminder to {row['chat_id']}: {e}")

        await bot.session.close()


async def scheduled_check():
    """Проверка расписания каждую минуту."""
    now = moscow_now()
    time_str = f"{now.hour:02d}:{now.minute:02d}"

    # Логируем каждые 10 минут для подтверждения работы scheduler
    if now.minute % 10 == 0:
        logger.info(f"[Scheduler] Проверка в {time_str} MSK")

    scheduled = await get_all_scheduled_interns(now.hour, now.minute)

    if scheduled:
        logger.info(f"[Scheduler] {time_str} MSK — найдено {len(scheduled)} пользователей для отправки")
        bot = Bot(token=_bot_token)
        me = await bot.get_me()
        logger.info(f"[Scheduler] Bot ID: {bot.id}, username: {me.username}")
        for chat_id, send_type in scheduled:
            try:
                if send_type in ('marathon', 'both'):
                    await send_scheduled_topic(chat_id, bot)
                if send_type in ('feed', 'both'):
                    await send_feed_notification(chat_id, bot)
                logger.info(f"[Scheduler] Sent {send_type} to {chat_id}")
            except Exception as e:
                error_msg = str(e).lower()
                if 'blocked' in error_msg or 'deactivated' in error_msg or 'chat not found' in error_msg:
                    logger.warning(f"[Scheduler] User {chat_id} blocked bot, skipping")
                else:
                    logger.error(f"[Scheduler] Ошибка отправки пользователю {chat_id}: {e}")
        await bot.session.close()

    # Проверяем напоминания
    await check_reminders()

    # Повторная отправка неотправленных заметок
    from clients.github_api import github_notes
    await github_notes.retry_pending()
