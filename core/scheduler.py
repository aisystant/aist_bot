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
from db.queries.marathon import save_marathon_content, get_marathon_content, cleanup_expired_content
from db.queries.users import moscow_now, moscow_today, get_marathon_users_at_time
from db.queries.feed import get_current_feed_week, get_feed_session, create_feed_session, expire_old_feed_sessions, update_feed_week
from i18n import t

logger = logging.getLogger(__name__)

# --- Module state ---
_scheduler: Optional[AsyncIOScheduler] = None
_aiogram_dispatcher = None  # aiogram Dispatcher (for FSM storage access)
_bot_dispatcher = None      # core.dispatcher.Dispatcher (for SM routing)
_bot_token: str = None


_RETRY_DELAYS_MINUTES = [30, 60]  # exponential backoff: 30min, then 60min


def _schedule_retry(chat_id: int, content_type: str, attempt: int = 0):
    """Schedule a one-off retry for failed pre-generation with exponential backoff."""
    if not _scheduler:
        return
    if attempt >= len(_RETRY_DELAYS_MINUTES):
        logger.warning(f"[Scheduler] Max retries ({len(_RETRY_DELAYS_MINUTES)}) exhausted for {chat_id} ({content_type})")
        return
    job_id = f"retry_{content_type}_{chat_id}"
    if _scheduler.get_job(job_id):
        logger.info(f"[Scheduler] Retry already pending for {chat_id} ({content_type}), skip")
        return
    delay = _RETRY_DELAYS_MINUTES[attempt]
    run_at = moscow_now() + timedelta(minutes=delay)
    _scheduler.add_job(
        _execute_retry,
        'date',
        run_date=run_at,
        id=job_id,
        args=[chat_id, content_type, attempt],
        replace_existing=True,
    )
    logger.info(f"[Scheduler] Retry #{attempt+1} scheduled for {chat_id} ({content_type}) at +{delay}min")


async def _execute_retry(chat_id: int, content_type: str, attempt: int = 0):
    """Execute a single retry for failed pre-generation."""
    bot = Bot(token=_bot_token)
    try:
        if content_type == 'marathon':
            await send_scheduled_topic(chat_id, bot)
        elif content_type == 'feed':
            await pre_generate_feed_digest(chat_id, bot)
        logger.info(f"[Scheduler] Retry #{attempt+1} successful for {chat_id} ({content_type})")
    except Exception as e:
        logger.error(f"[Scheduler] Retry #{attempt+1} failed for {chat_id} ({content_type}): {e}")
        _schedule_retry(chat_id, content_type, attempt + 1)
    finally:
        await bot.session.close()


def init_scheduler(bot_dispatcher, aiogram_dispatcher, bot_token: str) -> AsyncIOScheduler:
    """Инициализировать и вернуть планировщик.

    Args:
        bot_dispatcher: core.dispatcher.Dispatcher (SM routing)
        aiogram_dispatcher: aiogram Dispatcher (FSM storage)
        bot_token: Telegram bot token
    """
    # DISABLE_SCHEDULER=true — отключает scheduler (для тестовых инстансов с общей БД)
    if os.getenv("DISABLE_SCHEDULER", "false").lower() == "true":
        logger.info("[Scheduler] DISABLE_SCHEDULER=true — планировщик отключён")
        return None

    global _scheduler, _bot_dispatcher, _aiogram_dispatcher, _bot_token
    _bot_dispatcher = bot_dispatcher
    _aiogram_dispatcher = aiogram_dispatcher
    _bot_token = bot_token

    _scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    _scheduler.add_job(scheduled_check, 'cron', minute='*')
    _scheduler.add_job(pre_generate_upcoming, 'cron', minute='*')  # Pre-gen за 3ч до доставки
    _scheduler.add_job(_neon_keep_alive, 'cron', minute='*/4')  # Keep-alive каждые 4 мин
    _scheduler.start()

    logger.info("[Scheduler] Планировщик инициализирован (+ Neon keep-alive + pre-gen)")
    return _scheduler


PREGEN_HOURS_AHEAD = 3


async def _generate_and_save_content(chat_id: int, intern: dict, topic_index: int) -> bool:
    """Сгенерировать урок+вопрос+практику и сохранить в marathon_content.

    Извлечённая логика из send_scheduled_topic() — используется и для пре-генерации,
    и как fallback при доставке.

    Returns:
        True если урок успешно сгенерирован и сохранён.
    """
    from clients import claude, mcp_knowledge
    from core.topics import get_topic, get_topics_for_day, TOPICS

    topic = get_topic(topic_index)
    if not topic:
        return False

    bloom_level = intern.get('complexity_level', 1) or intern.get('bloom_level', 1) or 1

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

    # Валидация: error fallback возвращает строку ~60 символов вместо None
    if lesson_content is not None and len(lesson_content) < 200:
        logger.error(f"[PreGen] Lesson too short ({len(lesson_content)} chars) for {chat_id}, "
                     f"topic {topic_index} — likely error fallback")
        lesson_content = None

    if lesson_content is None:
        return False

    if isinstance(results[1], Exception):
        logger.warning(f"[PreGen] Question generation failed for {chat_id}: {results[1]}")
    if isinstance(results[2], Exception):
        logger.warning(f"[PreGen] Practice generation failed for {chat_id}: {results[2]}")

    # Сохраняем в БД
    await save_marathon_content(
        chat_id=chat_id,
        topic_index=topic_index,
        lesson_content=lesson_content,
        question_content=question_content,
        practice_content=practice_content,
        bloom_level=bloom_level,
    )

    # Pre-gen для парной темы того же дня (theory→practice)
    completed = set(intern.get('completed_topics', []))
    same_day_topics = get_topics_for_day(topic['day'])
    for pair_topic in same_day_topics:
        pair_idx = next(
            (i for i, t_item in enumerate(TOPICS) if t_item['id'] == pair_topic['id']),
            None,
        )
        if pair_idx is not None and pair_idx != topic_index and pair_idx not in completed:
            try:
                pair_results = await asyncio.wait_for(
                    asyncio.gather(
                        claude.generate_content(topic=pair_topic, intern=intern, mcp_client=mcp_knowledge),
                        claude.generate_question(topic=pair_topic, intern=intern, bloom_level=bloom_level),
                        claude.generate_practice_intro(topic=pair_topic, intern=intern),
                        return_exceptions=True,
                    ),
                    timeout=120,
                )
                await save_marathon_content(
                    chat_id=chat_id,
                    topic_index=pair_idx,
                    lesson_content=pair_results[0] if not isinstance(pair_results[0], Exception) else None,
                    question_content=pair_results[1] if not isinstance(pair_results[1], Exception) else None,
                    practice_content=pair_results[2] if not isinstance(pair_results[2], Exception) else None,
                    bloom_level=bloom_level,
                )
                logger.info(f"[PreGen] Pair content saved for {chat_id}, topic {pair_idx} (day {topic['day']})")
            except Exception as e:
                logger.warning(f"[PreGen] Pair pre-gen failed for {chat_id}, topic {pair_idx}: {e}")

    return True


async def pregen_next_for_user(chat_id: int, intern: dict, current_topic_index: int):
    """Look-ahead: пре-генерация следующей темы после текущей (fire-and-forget).

    Вызывается из lesson.py / task.py после доставки контента пользователю.
    Если следующая тема уже пре-генерирована — пропускаем.
    Rule 10.19: Look-ahead pre-gen при доставке контента.
    """
    from core.topics import get_available_topics

    try:
        available = get_available_topics(intern)
        # Найти следующие темы после текущей
        next_topics = [
            (idx, topic) for idx, topic in available
            if idx > current_topic_index
        ][:2]  # максимум 2 look-ahead

        if not next_topics:
            return

        for next_idx, _ in next_topics:
            existing = await get_marathon_content(chat_id, next_idx)
            if existing and existing.get('lesson_content') and len(existing['lesson_content']) > 200:
                continue  # уже есть
            success = await _generate_and_save_content(chat_id, intern, next_idx)
            if success:
                logger.info(f"[LookAhead] Pre-generated topic {next_idx} for {chat_id}")
            break  # генерируем только одну тему за раз (не блокировать API)

    except Exception as e:
        logger.warning(f"[LookAhead] Failed for {chat_id}: {e}")


async def pre_generate_upcoming():
    """Пре-генерация контента марафона за PREGEN_HOURS_AHEAD часов до доставки.

    Запускается каждую минуту. Находит пользователей, чьё schedule_time наступит
    через 3 часа, и генерирует контент заранее — чтобы в момент доставки
    не нагружать Claude API.
    """
    now = moscow_now()
    target = now + timedelta(hours=PREGEN_HOURS_AHEAD)

    users = await get_marathon_users_at_time(target.hour, target.minute)
    if not users:
        return

    from core.topics import get_next_topic_index

    logger.info(f"[PreGen] Found {len(users)} marathon users for {target.hour:02d}:{target.minute:02d} "
                f"(delivery in {PREGEN_HOURS_AHEAD}h)")

    sem = asyncio.Semaphore(20)

    async def _pregen_one(chat_id: int):
        async with sem:
            try:
                intern = await get_intern(chat_id)
                if not intern or intern.get('marathon_status') != 'active':
                    return

                topic_index = get_next_topic_index(intern)
                if topic_index is None:
                    return

                # Уже пре-генерирован?
                existing = await get_marathon_content(chat_id, topic_index)
                if existing and existing.get('status') == 'pending':
                    return

                success = await _generate_and_save_content(chat_id, intern, topic_index)
                if success:
                    logger.info(f"[PreGen] Content ready for {chat_id}, topic {topic_index}")
                else:
                    _schedule_retry(chat_id, 'marathon')
            except asyncio.TimeoutError:
                logger.error(f"[PreGen] Timeout for {chat_id}")
                _schedule_retry(chat_id, 'marathon')
            except Exception as e:
                logger.error(f"[PreGen] Error for {chat_id}: {e}")

    await asyncio.gather(*[_pregen_one(cid) for cid in users])


async def pre_generate_feed_digest(chat_id: int, bot: Bot):
    """Пре-генерация дайджеста Ленты и отправка уведомления.

    Паттерн аналогичен send_scheduled_topic() для Марафона:
    1. Validate feed active + week active
    2. Check: сессия на сегодня уже есть → skip
    3. Generate: generate_multi_topic_digest()
    4. Save: create_feed_session(status='pending')
    5. Send notification: кнопка «Получить дайджест»
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from engines.feed.planner import generate_multi_topic_digest
    from config import FeedWeekStatus, FEED_SESSION_DURATION_MAX, FEED_SESSION_DURATION_MIN

    intern = await get_intern(chat_id)
    if not intern:
        return

    lang = intern.get('language', 'ru') or 'ru'
    feed_status = intern.get('feed_status', 'not_started')

    if feed_status != 'active':
        return

    # Проверяем активную неделю
    week = await get_current_feed_week(chat_id)
    if not week:
        logger.info(f"[Scheduler] Feed: {chat_id} — no week, skip")
        return

    # Continuous mode: re-activate completed weeks
    if week.get('status') == FeedWeekStatus.COMPLETED:
        await update_feed_week(week['id'], {'status': FeedWeekStatus.ACTIVE})
        week['status'] = FeedWeekStatus.ACTIVE
        logger.info(f"[Scheduler] Feed: re-activated completed week {week['id']} for {chat_id}")

    if week.get('status') != FeedWeekStatus.ACTIVE:
        logger.info(f"[Scheduler] Feed: {chat_id} — week status {week.get('status')}, skip")
        return

    # Авто-экспайр незакрытых сессий за прошлые дни (замкнутый lifecycle)
    await expire_old_feed_sessions(chat_id)

    # Если сессия на сегодня уже есть — не генерируем повторно
    today = moscow_today()
    existing = await get_feed_session(week['id'], today)
    if existing:
        logger.info(f"[Scheduler] Feed: {chat_id} — session for today exists (status={existing.get('status')}), skip")
        return

    topics = week.get('accepted_topics', [])
    if not topics:
        logger.info(f"[Scheduler] Feed: {chat_id} — no topics selected, skip")
        return

    depth_level = week.get('current_day', 1)
    duration = intern.get('feed_duration', FEED_SESSION_DURATION_MAX)
    if not duration or duration < FEED_SESSION_DURATION_MIN:
        duration = (FEED_SESSION_DURATION_MIN + FEED_SESSION_DURATION_MAX) // 2

    # ─── Пре-генерация дайджеста ───
    try:
        content = await asyncio.wait_for(
            generate_multi_topic_digest(
                topics=topics,
                intern=intern,
                duration=duration,
                depth_level=depth_level,
            ),
            timeout=120,
        )

        if not content or not content.get('topics_detail'):
            logger.error(f"[Scheduler] Feed: digest generation returned empty for {chat_id}")
            _schedule_retry(chat_id, 'feed')
            return

        # Сохраняем как pending (не показана пользователю)
        topics_title = ", ".join(topics)
        await create_feed_session(
            week_id=week['id'],
            day_number=depth_level,
            topic_title=topics_title,
            content=content,
            session_date=today,
            status='pending',
        )
        logger.info(f"[Scheduler] Feed: pre-generated digest for {chat_id} "
                     f"(topics: {topics_title}, depth: {depth_level})")

    except asyncio.TimeoutError:
        logger.error(f"[Scheduler] Feed: pre-generation timeout (120s) for {chat_id}")
        _schedule_retry(chat_id, 'feed')
        return
    except Exception as e:
        logger.error(f"[Scheduler] Feed: pre-generation error for {chat_id}: {e}")
        _schedule_retry(chat_id, 'feed')
        return

    # Отправляем уведомление с кнопкой «Получить дайджест»
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

    Проверяет, пре-генерирован ли контент (за 3ч через pre_generate_upcoming).
    Если да — сразу уведомление. Если нет — fallback на генерацию сейчас.
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

    # НЕ обновляем current_topic_index здесь — scheduler только пре-генерирует контент.
    # current_topic_index обновляется в lesson.py при реальном взаимодействии пользователя.

    # ─── Проверяем: контент уже пре-генерирован (за 3h)? ───
    existing = await get_marathon_content(chat_id, topic_index)
    if existing and existing.get('status') == 'pending' and existing.get('lesson_content'):
        logger.info(f"[Scheduler] Pre-generated content found for {chat_id}, topic {topic_index} — skip generation")
    else:
        # Fallback: генерируем сейчас (контент не был пре-генерирован)
        try:
            success = await _generate_and_save_content(chat_id, intern, topic_index)
            if not success:
                logger.error(f"[Scheduler] Lesson generation failed for {chat_id}, topic {topic_index}")
                _schedule_retry(chat_id, 'marathon')
                return
            logger.info(f"[Scheduler] On-demand generation for {chat_id}, topic {topic_index}")
        except asyncio.TimeoutError:
            logger.error(f"[Scheduler] Pre-generation timeout (120s) for {chat_id}, topic {topic_index}")
            _schedule_retry(chat_id, 'marathon')
            return
        except Exception as e:
            logger.error(f"[Scheduler] Pre-generation error for {chat_id}: {e}")
            _schedule_retry(chat_id, 'marathon')
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

        async def _process_user(chat_id: int, send_type: str):
            """Обработка одного пользователя (marathon + feed)."""
            try:
                if send_type in ('marathon', 'both'):
                    await send_scheduled_topic(chat_id, bot)
                if send_type in ('feed', 'both'):
                    await pre_generate_feed_digest(chat_id, bot)
                logger.info(f"[Scheduler] Sent {send_type} to {chat_id}")
            except Exception as e:
                error_msg = str(e).lower()
                if 'blocked' in error_msg or 'deactivated' in error_msg or 'chat not found' in error_msg:
                    logger.warning(f"[Scheduler] User {chat_id} blocked bot, skipping")
                else:
                    logger.error(f"[Scheduler] Ошибка отправки пользователю {chat_id}: {e}", exc_info=True)

        # Параллельная обработка пользователей (max 40 одновременно)
        # Telegram rate limit: 30 msg/sec, но Claude генерация (5-10с) stagger-ит сообщения
        sem = asyncio.Semaphore(40)

        async def _bounded(chat_id, send_type):
            async with sem:
                await _process_user(chat_id, send_type)

        await asyncio.gather(*[_bounded(cid, st) for cid, st in scheduled])
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

    # 🚀 Launch day notification (23 фев, 10:00 MSK — одноразово)
    from config.settings import SUBSCRIPTION_LAUNCH_DATE
    if (now.date() == SUBSCRIPTION_LAUNCH_DATE
            and now.hour == 10 and now.minute == 0):
        try:
            await send_subscription_launch_notification()
        except Exception as e:
            logger.error(f"[Scheduler] Launch notification error: {e}")

    # ⭐ Trial expiry notifications (10:00 MSK daily, только после запуска)
    if (now.date() > SUBSCRIPTION_LAUNCH_DATE
            and now.hour == 10 and now.minute == 0):
        try:
            await send_trial_expiry_notifications()
        except Exception as e:
            logger.error(f"[Scheduler] Trial expiry notification error: {e}")

    # 🚨 Latency alert: проверяем каждые 15 минут
    if now.minute % 15 == 0 and dev_chat_id:
        try:
            from db.queries.traces import check_latency_alerts
            alert_text = await check_latency_alerts(minutes=15)
            if alert_text:
                bot = Bot(token=_bot_token)
                try:
                    await bot.send_message(int(dev_chat_id), alert_text, parse_mode="HTML")
                    logger.info("[Scheduler] Latency alert sent to developer")
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f"[Scheduler] Latency alert error: {e}")

    # 🚨 Error alert: проверяем каждые 15 минут
    if now.minute % 15 == 0 and dev_chat_id:
        try:
            from db.queries.errors import check_error_alerts
            alert_text = await check_error_alerts(minutes=15)
            if alert_text:
                bot = Bot(token=_bot_token)
                try:
                    await bot.send_message(int(dev_chat_id), alert_text, parse_mode="HTML")
                    logger.info("[Scheduler] Error alert sent to developer")
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f"[Scheduler] Error alert error: {e}")

    # 🧹 Midnight cleanup: удаляем невостребованный пре-генерированный контент + старые traces
    if now.hour == 0 and now.minute == 0:
        try:
            await cleanup_expired_content()
        except Exception as e:
            logger.error(f"[Scheduler] Midnight cleanup error: {e}")
        try:
            from db.queries.traces import cleanup_old_traces
            await cleanup_old_traces(days=7)
        except Exception as e:
            logger.error(f"[Scheduler] Traces cleanup error: {e}")
        try:
            from db.queries.errors import cleanup_old_errors
            await cleanup_old_errors(days=7)
        except Exception as e:
            logger.error(f"[Scheduler] Error logs cleanup error: {e}")
        try:
            from db.queries.cache import cache_cleanup
            await cache_cleanup()
        except Exception as e:
            logger.error(f"[Scheduler] Cache cleanup error: {e}")

        # Финализация устаревших сессий
        try:
            from db.queries.sessions import finalize_stale_sessions
            await finalize_stale_sessions()
        except Exception as e:
            logger.error(f"[Scheduler] Session cleanup error: {e}")

    # 🔧 Unstick: проверяем застрявших пользователей каждые 5 минут
    if now.minute % 5 == 0:
        try:
            from core.unstick import check_and_recover_users
            await check_and_recover_users()
        except Exception as e:
            logger.error(f"[Scheduler] Unstick check error: {e}")

    # 🤖 Hourly DT sync retry: проверяем подключённых пользователей, досинхронизируем
    if now.minute == 0:
        try:
            await _sync_dt_connected_users()
        except Exception as e:
            logger.error(f"[Scheduler] DT sync retry error: {e}")

    # Повторная отправка неотправленных заметок
    from clients.github_api import github_notes
    await github_notes.retry_pending()


# ═══════════════════════════════════════════════════════════
# NEON KEEP-ALIVE
# ═══════════════════════════════════════════════════════════

async def _neon_keep_alive():
    """Пинг Neon каждые 4 минуты — предотвращение idle timeout (cold start)."""
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval('SELECT 1')
    except Exception as e:
        logger.warning(f"[Scheduler] Neon keep-alive failed: {e}")


# ═══════════════════════════════════════════════════════════
# DIGITAL TWIN SYNC RETRY
# ═══════════════════════════════════════════════════════════

async def _sync_dt_connected_users():
    """Проверяет подключённых к ЦД пользователей и досинхронизирует профиль."""
    from clients.digital_twin import digital_twin
    from db.queries.users import get_intern

    connected_ids = digital_twin.get_connected_user_ids()
    if not connected_ids:
        return

    for user_id in connected_ids:
        try:
            intern = await get_intern(user_id)
            if intern:
                await digital_twin.sync_profile(user_id, intern)
        except Exception as e:
            logger.error(f"[DT Sync] Retry failed for user {user_id}: {e}")


# ═══════════════════════════════════════════════════════════
# SUBSCRIPTION LAUNCH NOTIFICATION
# ═══════════════════════════════════════════════════════════

async def send_subscription_launch_notification():
    """Одноразовое уведомление всем пользователям о запуске подписки."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from core.pricing import get_current_price
    from db import get_pool

    price = get_current_price()
    bot = Bot(token=_bot_token)

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT chat_id FROM interns WHERE onboarding_completed = TRUE'
            )

        sent = 0
        for row in rows:
            chat_id = row['chat_id']
            intern = await get_intern(chat_id)
            lang = intern.get('language', 'ru') or 'ru'

            text = t('subscription.launch_notification', lang, price=price)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('subscription.subscribe_button', lang, price=price),
                    callback_data="subscribe",
                )]
            ])

            try:
                await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")
                sent += 1
            except Exception as e:
                error_msg = str(e).lower()
                if 'blocked' not in error_msg and 'deactivated' not in error_msg:
                    logger.error(f"[Scheduler] Launch notification error for {chat_id}: {e}")

        logger.info(f"[Scheduler] Subscription launch notification sent to {sent}/{len(rows)} users")
    finally:
        await bot.session.close()


# ═══════════════════════════════════════════════════════════
# TRIAL EXPIRY NOTIFICATIONS
# ═══════════════════════════════════════════════════════════

async def send_trial_expiry_notifications():
    """Уведомить пользователей, чей триал истекает через 1 день или сегодня."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from core.pricing import get_current_price
    from db.queries.subscription import get_trial_expiring_users

    price = get_current_price()
    bot = Bot(token=_bot_token)

    try:
        for days_ahead in [1, 0]:
            chat_ids = await get_trial_expiring_users(days_ahead)
            for chat_id in chat_ids:
                intern = await get_intern(chat_id)
                lang = intern.get('language', 'ru') or 'ru'

                if days_ahead == 1:
                    text = t('subscription.trial_expiring', lang)
                else:
                    text = t('subscription.trial_expired', lang)

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=t('subscription.subscribe_button', lang, price=price),
                        callback_data="subscribe",
                    )]
                ])

                try:
                    await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")
                    logger.info(f"[Scheduler] Trial expiry notification sent to {chat_id} (days_ahead={days_ahead})")
                except Exception as e:
                    error_msg = str(e).lower()
                    if 'blocked' not in error_msg and 'deactivated' not in error_msg:
                        logger.error(f"[Scheduler] Trial notification error for {chat_id}: {e}")
    finally:
        await bot.session.close()


# ═══════════════════════════════════════════════════════════
# ДАЙДЖЕСТЫ ОБРАТНОЙ СВЯЗИ
# ═══════════════════════════════════════════════════════════

async def send_feedback_daily_digest(dev_chat_id: int):
    """Отправить 🟡 ежедневный дайджест жёлтых отчётов."""
    from db.queries.feedback import get_pending_reports, mark_notified, format_user_label

    reports = await get_pending_reports(severity='yellow', since_hours=24)
    if not reports:
        return

    bot = Bot(token=_bot_token)
    lines = [f"\U0001f7e1 <b>{len(reports)} новых отчётов за день:</b>\n"]
    for r in reports:
        scenario = r.get('scenario', 'other')
        msg = (r.get('message', '') or '')[:60]
        lines.append(f"\u2022 #{r['id']} | {format_user_label(r)} | {scenario} | \"{msg}\"")
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
    from db.queries.feedback import get_pending_reports, mark_notified, format_user_label

    reports = await get_pending_reports(severity='green', since_hours=168)
    if not reports:
        return

    bot = Bot(token=_bot_token)
    lines = [f"\U0001f7e2 <b>{len(reports)} предложений за неделю:</b>\n"]
    for r in reports:
        msg = (r.get('message', '') or '')[:60]
        lines.append(f"\u2022 #{r['id']} | {format_user_label(r)} | \"{msg}\"")
    text = "\n".join(lines)

    try:
        await bot.send_message(dev_chat_id, text, parse_mode="HTML")
        await mark_notified([r['id'] for r in reports])
        logger.info(f"[Scheduler] Sent feedback weekly digest: {len(reports)} reports")
    except Exception as e:
        logger.error(f"[Scheduler] Feedback weekly digest error: {e}")
    finally:
        await bot.session.close()
