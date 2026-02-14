"""
Планировщик — отправка тем по расписанию и напоминания.

Извлечён из bot.py. Использует core.dispatcher для SM-роутинга.
"""

import asyncio
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
from db.queries.marathon import save_marathon_content, cleanup_expired_content
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
    """Отправка уведомления о готовности урока марафона по расписанию.

    Вместо прямой генерации контента — отправляем уведомление
    с кнопкой «Получить урок» (аналогично Feed-дайджесту).
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from core.topics import get_marathon_day, get_next_topic_index, get_topic, get_total_topics, get_lessons_tasks_progress
    from core.knowledge import get_topic_title

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

    # ─── Пре-генерация контента (урок + вопрос + практика) ───
    from clients import claude, mcp_knowledge

    bloom_level = intern.get('complexity_level', 1) or intern.get('bloom_level', 1) or 1

    try:
        # Генерируем все 3 типа параллельно
        lesson_task = claude.generate_content(
            topic=topic, intern=intern, mcp_client=mcp_knowledge
        )
        question_task = claude.generate_question(
            topic=topic, intern=intern, bloom_level=bloom_level
        )
        practice_task = claude.generate_practice_intro(
            topic=topic, intern=intern
        )

        results = await asyncio.wait_for(
            asyncio.gather(lesson_task, question_task, practice_task, return_exceptions=True),
            timeout=120,
        )

        lesson_content = results[0] if not isinstance(results[0], Exception) else None
        question_content = results[1] if not isinstance(results[1], Exception) else None
        practice_content = results[2] if not isinstance(results[2], Exception) else None

        if lesson_content is None:
            logger.error(f"[Scheduler] Lesson generation failed for {chat_id}, topic {topic_index}: {results[0]}")
            # Без урока уведомление бессмысленно — пропускаем
            return

        if isinstance(results[1], Exception):
            logger.warning(f"[Scheduler] Question generation failed for {chat_id}: {results[1]}")
        if isinstance(results[2], Exception):
            logger.warning(f"[Scheduler] Practice generation failed for {chat_id}: {results[2]}")

        # Сохраняем в БД
        await save_marathon_content(
            chat_id=chat_id,
            topic_index=topic_index,
            lesson_content=lesson_content,
            question_content=question_content,
            practice_content=practice_content,
            bloom_level=bloom_level,
        )
        logger.info(f"[Scheduler] Pre-generated content for {chat_id}, topic {topic_index} "
                     f"(lesson: ✅, question: {'✅' if question_content else '❌'}, "
                     f"practice: {'✅' if practice_content else '❌'})")

    except asyncio.TimeoutError:
        logger.error(f"[Scheduler] Pre-generation timeout (120s) for {chat_id}, topic {topic_index}")
        return
    except Exception as e:
        logger.error(f"[Scheduler] Pre-generation error for {chat_id}: {e}")
        return

    # Планируем напоминания (+1ч и +3ч)
    await schedule_reminders(chat_id, intern)

    # Отправляем уведомление с кнопкой «Получить урок»
    topic_title = get_topic_title(topic, lang)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📚 {t('buttons.get_lesson', lang)}",
            callback_data="marathon_get_lesson"
        )]
    ])

    await bot.send_message(
        chat_id,
        f"*{t('reminders.marathon_lesson_ready', lang)}*\n"
        f"📚 {topic_title}\n\n"
        f"{t('reminders.marathon_lesson_cta', lang)}",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    logger.info(f"[Scheduler] Sent marathon notification to {chat_id}, topic: {topic_title}")


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
    """Отправляет напоминание с кнопкой «Получить урок»."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📚 {t('buttons.get_lesson', lang)}",
            callback_data="marathon_get_lesson"
        )]
    ])

    if reminder_type == '+1h':
        await bot.send_message(
            chat_id,
            f"⏰ *{t('reminders.title', lang)}*\n\n"
            f"{t('reminders.day_waiting', lang, day=marathon_day)}\n\n"
            f"{t('reminders.two_topics_today', lang)}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    elif reminder_type == '+3h':
        await bot.send_message(
            chat_id,
            f"🔔 *{t('reminders.last_reminder', lang)}*\n\n"
            f"{t('reminders.day_not_started', lang, day=marathon_day)}\n\n"
            f"{t('reminders.regularity_tip', lang)}\n"
            f"{t('reminders.even_15_min', lang)}",
            reply_markup=keyboard,
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

    # Дайджесты обратной связи для разработчика
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    if dev_chat_id:
        try:
            dev_id = int(dev_chat_id)
            # 🟡 Ежедневный дайджест в 21:00 MSK
            if now.hour == 21 and now.minute == 0:
                await send_feedback_daily_digest(dev_id)
            # 🟢 Еженедельный дайджест Пн 10:00 MSK
            if now.weekday() == 0 and now.hour == 10 and now.minute == 0:
                await send_feedback_weekly_digest(dev_id)
        except (ValueError, Exception) as e:
            logger.error(f"[Scheduler] Feedback digest error: {e}")

    # 🧹 Midnight cleanup: удаляем невостребованный пре-генерированный контент
    if now.hour == 0 and now.minute == 0:
        try:
            await cleanup_expired_content()
        except Exception as e:
            logger.error(f"[Scheduler] Midnight cleanup error: {e}")

    # Повторная отправка неотправленных заметок
    from clients.github_api import github_notes
    await github_notes.retry_pending()


# ═══════════════════════════════════════════════════════════
# ДАЙДЖЕСТЫ ОБРАТНОЙ СВЯЗИ
# ═══════════════════════════════════════════════════════════

async def send_feedback_daily_digest(dev_chat_id: int):
    """Отправить 🟡 ежедневный дайджест жёлтых отчётов."""
    from db.queries.feedback import get_pending_reports, mark_notified

    reports = await get_pending_reports(severity='yellow', since_hours=24)
    if not reports:
        return

    bot = Bot(token=_bot_token)
    lines = [f"\U0001f7e1 <b>{len(reports)} новых отчётов за день:</b>\n"]
    for r in reports:
        scenario = r.get('scenario', 'other')
        msg = (r.get('message', '') or '')[:60]
        lines.append(f"\u2022 #{r['id']} | {scenario} | \"{msg}\"")
    text = "\n".join(lines)

    try:
        await bot.send_message(dev_chat_id, text, parse_mode="HTML")
        await mark_notified([r['id'] for r in reports])
        logger.info(f"[Scheduler] Sent feedback daily digest: {len(reports)} reports")
    except Exception as e:
        logger.error(f"[Scheduler] Feedback daily digest error: {e}")
    finally:
        await bot.session.close()


async def send_feedback_weekly_digest(dev_chat_id: int):
    """Отправить 🟢 еженедельный дайджест предложений."""
    from db.queries.feedback import get_pending_reports, mark_notified

    reports = await get_pending_reports(severity='green', since_hours=168)
    if not reports:
        return

    bot = Bot(token=_bot_token)
    lines = [f"\U0001f7e2 <b>{len(reports)} предложений за неделю:</b>\n"]
    for r in reports:
        msg = (r.get('message', '') or '')[:60]
        lines.append(f"\u2022 #{r['id']} | \"{msg}\"")
    text = "\n".join(lines)

    try:
        await bot.send_message(dev_chat_id, text, parse_mode="HTML")
        await mark_notified([r['id'] for r in reports])
        logger.info(f"[Scheduler] Sent feedback weekly digest: {len(reports)} reports")
    except Exception as e:
        logger.error(f"[Scheduler] Feedback weekly digest error: {e}")
    finally:
        await bot.session.close()
