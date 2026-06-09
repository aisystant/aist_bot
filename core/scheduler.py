from __future__ import annotations

"""
Планировщик — отправка тем по расписанию и напоминания.

Извлечён из bot.py. Использует core.dispatcher для SM-роутинга.
"""

import asyncio
import html
import json
import logging
import math
import os
import random
import re
from collections import Counter
from datetime import date, datetime, timedelta

# WP-330 С5 watchdog active window (МСК). После 31 мая функция auto-noop.
_WP330_C5_WATCHDOG_DATE = date(2026, 5, 31)
from typing import Optional

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import MOSCOW_TZ, MAX_TOPICS_PER_DAY, MARATHON_DAYS, MarathonStatus, MENTOR_CHANNEL_ID
from db.connection import get_pool
from db.queries import get_intern, update_intern, get_all_scheduled_interns, get_topics_today
from db.queries.users import derive_mode
from db.queries.marathon import save_marathon_content, get_marathon_content, mark_notification_sent, cleanup_expired_content, cleanup_error_questions
from db.queries.marathon_newcomer import is_on_newcomer_marathon
from db.queries.users import moscow_now, moscow_today, get_marathon_users_at_time
from db.queries.feed import get_current_feed_week, get_feed_session, create_feed_session, expire_old_feed_sessions, update_feed_week
from i18n import t
from clients.claude import is_api_degraded

logger = logging.getLogger(__name__)

# --- Module state ---
_scheduler: Optional[AsyncIOScheduler] = None
_aiogram_dispatcher = None  # aiogram Dispatcher (for FSM storage access)
_bot_dispatcher = None      # core.dispatcher.Dispatcher (for SM routing)
_bot_token: str = None
_bot_id: int = None          # Telegram bot ID — used to isolate reminders on shared DB

# WP-7 Ф-Bot-RateLimit: глобальный semaphore для MarathonQueue (≤20 msg/sec)
_marathon_semaphore = asyncio.Semaphore(5)

# WP-330 P1-2: warn once if MENTOR_CHANNEL_ID is not configured
_MENTOR_CHANNEL_WARNED = False


def _warn_if_no_mentor_channel():
    global _MENTOR_CHANNEL_WARNED
    if not MENTOR_CHANNEL_ID and not _MENTOR_CHANNEL_WARNED:
        logger.warning(
            "[MarathonQueue] MENTOR_CHANNEL_ID not set, mentor alerts disabled. "
            "Set env var to enable alerts for failed deliveries and missed checkins."
        )
        _MENTOR_CHANNEL_WARNED = True


_RETRY_DELAYS_MINUTES = [30, 60]  # exponential backoff: 30min, then 60min


def _schedule_retry(chat_id: int, content_type: str, attempt: int = 0):
    """Schedule a one-off retry for failed pre-generation with exponential backoff."""
    if not _scheduler:
        return
    if is_api_degraded():
        # Reschedule after pause ends + 5 min buffer instead of dropping.
        from clients.claude import get_api_pause_remaining
        delay_sec = get_api_pause_remaining() + 300  # 5 min buffer
        if delay_sec > 3600:
            delay_sec = 3600  # cap at 1h
        run_at = moscow_now() + timedelta(seconds=delay_sec)
        job_id = f"retry_{content_type}_{chat_id}"
        _scheduler.add_job(
            _execute_retry,
            'date',
            run_date=run_at,
            id=job_id,
            args=[chat_id, content_type, attempt],
            replace_existing=True,
        )
        logger.info(f"[Scheduler] API degraded, retry for {chat_id} ({content_type}) rescheduled to +{delay_sec:.0f}s")
        return
    if attempt >= len(_RETRY_DELAYS_MINUTES):
        logger.warning(f"[Scheduler] Max retries ({len(_RETRY_DELAYS_MINUTES)}) exhausted for {chat_id} ({content_type})")
        _scheduler.add_job(
            _notify_retry_exhausted,
            'date',
            run_date=moscow_now(),
            id=f"notify_exhausted_{chat_id}_{content_type}",
            replace_existing=True,
            args=[chat_id, content_type],
        )
        return
    job_id = f"retry_{content_type}_{chat_id}"
    if _scheduler.get_job(job_id):
        logger.info(f"[Scheduler] Retry already pending for {chat_id} ({content_type}), skip")
        return
    delay = _RETRY_DELAYS_MINUTES[attempt] * random.uniform(0.75, 1.25)
    run_at = moscow_now() + timedelta(minutes=delay)
    _scheduler.add_job(
        _execute_retry,
        'date',
        run_date=run_at,
        id=job_id,
        args=[chat_id, content_type, attempt],
        replace_existing=True,
    )
    logger.info(f"[Scheduler] Retry #{attempt+1} scheduled for {chat_id} ({content_type}) at +{delay:.1f}min")


async def _execute_retry(chat_id: int, content_type: str, attempt: int = 0):
    """Execute a single retry for failed pre-generation.

    'tailor' content_type удалён 11 мая 2026 (WP-301): персональное руководство
    больше не доставляется через бот, перешло на git-канал.
    """
    if is_api_degraded():
        logger.info(f"[Scheduler] API degraded, reschedule retry execution for {chat_id} ({content_type})")
        _schedule_retry(chat_id, content_type, attempt)
        return
    bot = Bot(token=_bot_token)
    try:
        if content_type == 'marathon':
            deferred = await send_scheduled_topic(chat_id, bot) or False
            if deferred:
                _schedule_retry(chat_id, content_type, attempt + 1)
                return
        elif content_type == 'feed':
            await pre_generate_feed_digest(chat_id, bot)
        elif content_type == 'tailor':
            logger.warning(f"[Scheduler] 'tailor' retry skipped — moved to git channel (WP-301)")
            return
        logger.info(f"[Scheduler] Retry #{attempt+1} successful for {chat_id} ({content_type})")
    except Exception as e:
        logger.error(f"[Scheduler] Retry #{attempt+1} failed for {chat_id} ({content_type}): {e}")
        _schedule_retry(chat_id, content_type, attempt + 1)
    finally:
        await bot.session.close()


async def _notify_retry_exhausted(chat_id: int, content_type: str):
    """Уведомить пользователя, когда все попытки retry исчерпаны.

    §10.10 log-before-send: записываем факт ДО отправки (защита от дублей).
    Bot-blocked guard через is_suppressed() из error_classifier.
    """
    from aiogram import Bot
    from core.error_classifier import is_suppressed

    if not _bot_token:
        return

    intern = await get_intern(chat_id)
    if not intern:
        return
    lang = intern.get('language', 'ru') or 'ru'

    if content_type == 'marathon':
        key = 'scheduler.retry_exhausted_marathon'
    else:
        key = 'scheduler.retry_exhausted_feed'

    text = t(key, lang)
    logger.info(f"[Scheduler] Notifying {chat_id} retry exhausted ({content_type}): {text!r}")

    # Персистентный маркер: предотвращает новый retry-цикл после exhaustion (F2)
    try:
        from db.connection import get_pool as _get_main_pool
        _pool = await _get_main_pool()
        async with _pool.acquire() as _conn:
            await _conn.execute(
                "UPDATE development.user_state SET retry_exhausted_date = CURRENT_DATE WHERE chat_id = $1",
                chat_id
            )
        logger.info(f"[Scheduler] retry_exhausted_date set for {chat_id} ({content_type})")
    except Exception as e:
        logger.warning(f"[Scheduler] Failed to set retry_exhausted_date for {chat_id}: {e}")

    bot = Bot(token=_bot_token)
    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        if not is_suppressed(__name__, str(e)):
            logger.error(f"[Scheduler] Failed to notify {chat_id} retry exhausted: {e}")
    finally:
        await bot.session.close()


async def _claude_health_probe():
    """Синтетический probe Claude API (каждые 5 мин).

    Не вызывает record_api_degradation() напрямую — _api_call() внутри
    health_check() уже регистрирует деградацию при ClientError/5xx/TimeoutError.
    Probe = чистый наблюдатель.
    """
    if is_api_degraded():
        logger.debug("[HealthProbe] API already degraded, skip probe")
        return

    from clients.claude import ClaudeClient
    client = ClaudeClient()
    ok = await client.health_check()
    if not ok:
        logger.warning("[HealthProbe] Claude API probe failed")


# ════════════════════════════════════════════════════════════════════
# WP-330: Marathon for newcomers — queue worker (cron every 10 min)
# ════════════════════════════════════════════════════════════════════

async def _process_marathon_queue():
    """Разбор очереди марафона новичков. Запускается каждые 10 минут.

    1. Выбирает pending-записи из learning.marathon_queue (scheduled_at <= NOW)
    2. Отправляет контент через Telegram Bot API
    3. Обновляет status → sent / retry / failed
    4. При 3+ failed — алерт в канал наставников (TODO: интеграция с WP-341)
    """
    from aiogram import Bot
    from db.queries.marathon_newcomer import (
        get_pending_queue_items,
        mark_queue_sent,
        schedule_queue_retry,
        mark_queue_failed,
        get_or_create_progress,
        update_progress,
    )

    if not _bot_token:
        logger.warning("[MarathonQueue] _bot_token not set, skip")
        return

    _warn_if_no_mentor_channel()

    items = await get_pending_queue_items(limit=100)
    if not items:
        return

    # P0: filter out blocked users to eliminate L1 noise (3× burst per blocked user)
    try:
        blocked_ids = await _get_blocked_chat_ids()
    except Exception as e:
        logger.warning("[MarathonQueue] _get_blocked_chat_ids failed: %s — processing without block filter", e)
        blocked_ids = set()
    if blocked_ids:
        items = [item for item in items if item['user_id'] not in blocked_ids]
    if not items:
        return

    bot = Bot(token=_bot_token)
    try:
        # Burst guard: максимум 1 lesson_practice на пользователя за запуск.
        # Если у пользователя несколько lesson_practice (retry старого дня +
        # нормальный новый день), отправляем только самый свежий (максимальный
        # day_number). Retry старого дня помечаем failed='superseded'.
        lesson_delivered_day: dict[int, int] = {}
        for item in items:
            async with _marathon_semaphore:
                try:
                    queue_id = item['id']
                    chat_id = item['user_id']
                    day = item['day_number']
                    content_type = item['content_type']
                    content_ref = item.get('content_ref')
                    content_text = item.get('content_text')
                    attempts = item['attempts']

                    if content_type == 'lesson_practice':
                        prev_day = lesson_delivered_day.get(chat_id)
                        if prev_day is not None:
                            if day < prev_day:
                                logger.info(
                                    "[MarathonQueue] Superseded: skip retry day %s for %s (delivered day %s)",
                                    day, chat_id, prev_day,
                                )
                                await mark_queue_failed(queue_id, "superseded_by_higher_day")
                            elif day == prev_day:
                                logger.info(
                                    "[MarathonQueue] Duplicate: skip day %s for %s",
                                    day, chat_id,
                                )
                                await mark_queue_failed(queue_id, "duplicate")
                            else:
                                # day > prev_day: retry старого дня пришёл раньше нового по scheduled_at.
                                # Уже отправили один урок в этом запуске — откладываем до следующего тика.
                                logger.warning(
                                    "[MarathonQueue] Burst guard: skip day %s for %s"
                                    " (already sent day %s this run, will retry next tick)",
                                    day, chat_id, prev_day,
                                )
                            continue
                        lesson_delivered_day[chat_id] = day

                    # WP-330 Ф10.C: split lesson_practice на 2 сообщения для новых записей
                    # (content_text=NULL). Legacy (content_text!=NULL) идут старым путём ниже.
                    if content_type == 'lesson_practice' and not content_text:
                        from core.marathon_content import get_day_text
                        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                        # WP-330 С9a: подаём intern в get_day_text для routing 4 версий
                        intern_for_routing = await get_intern(chat_id)
                        lesson = (
                            get_day_text(day, 'lesson', intern=intern_for_routing)
                            or get_day_text(day, 'lesson')
                        )
                        faq = get_day_text(day, 'faq_hint')
                        if lesson:
                            lesson_text = lesson + (f"\n\n{faq}" if faq else "")
                            # Length guard для future long_complex (Шаг E)
                            if len(lesson_text) > 4000:
                                lesson_text = lesson_text[:3990] + "\n\n…"
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(
                                    text="✏️ Перейти к практике",
                                    callback_data=f"marathon_practice:{day}"
                                )
                            ]])
                            try:
                                await bot.send_message(chat_id, lesson_text, parse_mode="Markdown", reply_markup=keyboard)
                                await mark_queue_sent(queue_id)
                                progress = await get_or_create_progress(chat_id)
                                current_day = progress.get('current_day', 0)
                                new_day = max(current_day, day)
                                if new_day != current_day:
                                    await update_progress(chat_id, current_day=new_day)
                                    logger.info(f"[MarathonQueue] Updated current_day {current_day}→{new_day} for {chat_id}")
                                logger.info(f"[MarathonQueue] Sent lesson_practice (split) day {day} to {chat_id}")
                            except Exception as e:
                                error_msg = str(e)
                                from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
                                if isinstance(e, TelegramRetryAfter):
                                    retry_after = getattr(e, 'retry_after', 30)
                                    delay_minutes = math.ceil(retry_after / 60)
                                    logger.warning(
                                        f"[MarathonQueue] Rate limit (split) for {chat_id}, "
                                        f"retry_after={retry_after}s, reschedule in {delay_minutes}min"
                                    )
                                    await schedule_queue_retry(queue_id, attempts, delay_minutes=delay_minutes)
                                    await asyncio.sleep(min(retry_after, 10))
                                    continue
                                if isinstance(e, TelegramForbiddenError) or _is_user_unavailable(e):
                                    logger.info(f"[MarathonQueue] Skipped {chat_id} (blocked) split lesson day {day}")
                                    await _handle_unavailable_user(chat_id, "marathon split lesson")
                                    await mark_queue_failed(queue_id, error_msg[:200])
                                    continue
                                logger.error(
                                    f"[MarathonQueue] Failed to send split lesson day {day} to {chat_id}: "
                                    f"{type(e).__name__}: {error_msg} | repr={repr(e)[:400]}"
                                )
                                if attempts >= 2:  # 3-я попытка (0,1,2) → failed
                                    await mark_queue_failed(queue_id, error_msg[:200])
                                    if MENTOR_CHANNEL_ID:
                                        try:
                                            await bot.send_message(
                                                MENTOR_CHANNEL_ID,
                                                f"🚨 *Алерт марафона*\n\n"
                                                f"Не удалось отправить урок участнику `{chat_id}`\n"
                                                f"День {day}, split lesson\n"
                                                f"Ошибка: `{error_msg[:200]}`",
                                                parse_mode="Markdown",
                                            )
                                        except Exception as alert_err:
                                            logger.warning(f"[MarathonQueue] Failed to send mentor alert: {alert_err}")
                                else:
                                    delay_minutes = min(30 * (2 ** attempts), 120)
                                    await schedule_queue_retry(queue_id, attempts, delay_minutes=delay_minutes)
                            continue  # пропускаем старый путь
                        # Иначе fallthrough на старый путь (safety net)

                    # Формируем текст сообщения (WP-330 С9a: intern для legacy fallback routing)
                    intern_for_build = await get_intern(chat_id) if not content_text else None
                    text = _build_marathon_message(content_type, day, content_ref, content_text, intern=intern_for_build)
                    if not text:
                        logger.warning(f"[MarathonQueue] Empty text for {chat_id} day {day} {content_type}, skip")
                        await mark_queue_failed(queue_id, "empty_text")
                        continue

                    try:
                        if content_type == 'checkin':
                            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [
                                    InlineKeyboardButton(text="😵 Хаос", callback_data=f"marathon_checkin:chaos:{day}"),
                                    InlineKeyboardButton(text="🧱 Тупик", callback_data=f"marathon_checkin:stuck:{day}"),
                                    InlineKeyboardButton(text="🔁 Поворот", callback_data=f"marathon_checkin:turn:{day}"),
                                ]
                            ])
                            await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)
                        else:
                            await bot.send_message(chat_id, text, parse_mode="Markdown")
                        await mark_queue_sent(queue_id)
                        if content_type == 'lesson_practice':
                            progress = await get_or_create_progress(chat_id)
                            current_day = progress.get('current_day', 0)
                            new_day = max(current_day, day)
                            if new_day != current_day:
                                await update_progress(chat_id, current_day=new_day)
                                logger.info(f"[MarathonQueue] Updated current_day {current_day}→{new_day} for {chat_id}")
                        logger.info(f"[MarathonQueue] Sent {content_type} day {day} to {chat_id}")
                    except Exception as e:
                        error_msg = str(e)

                        # --- Specific Telegram error handling ---
                        from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError

                        if isinstance(e, TelegramForbiddenError):
                            # TODO: нет механизма реактивации. Пользователь может разблокировать
                            # бота позже — тогда нужен фоновый periodic re-check или manual
                            # /unblock команда. Сейчас помечаем unavailable навсегда.
                            logger.info(f"[MarathonQueue] Skipped {chat_id} (blocked) {content_type} day {day}")
                            await _handle_unavailable_user(chat_id, f"marathon {content_type} day {day}")
                            await mark_queue_failed(queue_id, f"forbidden: {error_msg[:200]}")
                            continue

                        if isinstance(e, TelegramRetryAfter):
                            retry_after = getattr(e, 'retry_after', 30)
                            delay_minutes = math.ceil(retry_after / 60)
                            logger.warning(
                                f"[MarathonQueue] Rate limit for {chat_id}, retry_after={retry_after}s, "
                                f"reschedule in {delay_minutes}min"
                            )
                            await schedule_queue_retry(queue_id, attempts, delay_minutes=delay_minutes)
                            # Реактивный guard: подождать retry_after перед следующей отправкой
                            # (чтобы не спровоцировать следующий flood для других сообщений)
                            await asyncio.sleep(min(retry_after, 10))
                            continue

                        if _is_user_unavailable(e):
                            logger.info(f"[MarathonQueue] Skipped {chat_id} (unavailable) {content_type} day {day}")
                            await _handle_unavailable_user(chat_id, f"marathon {content_type} day {day}")
                            await mark_queue_failed(queue_id, f"user_unavailable: {error_msg[:200]}")
                            continue

                        logger.error(
                            f"[MarathonQueue] Failed to send {content_type} day {day} to {chat_id}: "
                            f"{type(e).__name__}: {error_msg} | repr={repr(e)[:400]}"
                        )
                        if attempts >= 2:  # 3-я попытка (0,1,2)
                            await mark_queue_failed(queue_id, error_msg[:500])
                            # WP-330 P1: алерт в канал наставников
                            if MENTOR_CHANNEL_ID:
                                try:
                                    await bot.send_message(
                                        MENTOR_CHANNEL_ID,
                                        f"🚨 *Алерт марафона*\n\n"
                                        f"Не удалось отправить сообщение участнику `{chat_id}`\n"
                                        f"День {day}, тип: {content_type}\n"
                                        f"Ошибка: `{error_msg[:200]}`",
                                        parse_mode="Markdown",
                                    )
                                except Exception as alert_err:
                                    logger.warning(f"[MarathonQueue] Failed to send mentor alert: {alert_err}")
                            logger.warning(f"[MarathonQueue] Max attempts reached for {chat_id} day {day} {content_type}")
                        else:
                            # Exponential backoff: 30min, 60min, 120min max
                            delay_minutes = min(30 * (2 ** attempts), 120)
                            await schedule_queue_retry(queue_id, attempts, delay_minutes=delay_minutes)
                except Exception as e:
                    logger.exception(f"[MarathonQueue] Unhandled error for item {item.get('id')}: {e}")
                    continue
            # WP-7 Ф-Bot-RateLimit: ≤20 msg/sec глобально для MarathonQueue
            # Sleep за пределами семафора — не блокируем слоты (peer-review fix)
            await asyncio.sleep(0.05)
    finally:
        await bot.session.close()


async def _check_marathon_missed_checkins():
    """WP-330 P1: проверить пропуски чек-инов и отправить алерты наставникам.

    Запускается каждые 6 часов. Находит активных участников с >= 2 пропущенными
    днями в окне [current_day-2 .. current_day] через marathon_state (не через
    разность колонок). Отправляет алерт в MENTOR_CHANNEL_ID.
    Деdup через notification_log: один алерт на участника в день (§10.10).
    """
    from db.queries.marathon_newcomer import get_missed_checkin_users
    from db.queries.notifications import try_insert_notification

    if not MENTOR_CHANNEL_ID or not _bot_token:
        _warn_if_no_mentor_channel()
        return

    users = await get_missed_checkin_users(min_days=2)
    if not users:
        return

    bot = Bot(token=_bot_token)
    now = moscow_now()
    today_str = now.strftime('%Y-%m-%d')
    try:
        for user in users:
            chat_id = user['user_id']
            current_day = user['current_day']
            total_checkins = user['total_checkins']
            missed = user.get('missed', max(0, current_day - total_checkins))

            # Один алерт в день на участника (§10.10 dedup)
            alert_key = f"marathon_mentor_alert:{chat_id}:{today_str}"
            if not await try_insert_notification(chat_id, 'marathon_mentor_alert', alert_key):
                continue

            try:
                await bot.send_message(
                    MENTOR_CHANNEL_ID,
                    f"⚠️ *Марафон: пропуски*\n\n"
                    f"Участник `{chat_id}` пропустил чек-ин {missed} дней подряд\n"
                    f"Текущий день: {current_day}/14, чек-инов: {total_checkins}",
                    parse_mode="Markdown",
                )
                logger.info(f"[MarathonMissed] Alert sent for user {chat_id} ({missed} days missed)")
            except Exception as e:
                logger.warning(f"[MarathonMissed] Failed to send alert for {chat_id}: {e}")
    finally:
        await bot.session.close()


async def _send_marathon_nudges():
    """WP-330 P2: отправить поддерживающие nudge участникам с пропусками чек-инов.

    Запускается ежедневно в 10:00 MSK. Защита от дублей через notification_log.
    """
    from db.queries.marathon_newcomer import get_users_for_nudge
    from db.queries.notifications import try_insert_notification

    if not _bot_token:
        return

    users = await get_users_for_nudge()
    if not users:
        return

    bot = Bot(token=_bot_token)
    try:
        now = moscow_now()
        today_str = now.strftime('%Y-%m-%d')

        for user in users:
            chat_id = user['user_id']
            current_day = user['current_day']
            total_checkins = user['total_checkins']
            missed = user.get('missed', max(0, current_day - total_checkins))

            # Защита от дублей: один nudge в день
            nudge_key = f"marathon_nudge:{chat_id}:{today_str}"
            if not await try_insert_notification(chat_id, 'marathon_nudge', nudge_key):
                continue

            if missed == 1:
                text = (
                    "🌤 *Небольшая пауза*\n\n"
                    "Вчера не получилось чекинуться — ничего страшного. "
                    "Сегодня новый день и новый слот. Попробуем снова?\n\n"
                    "Если что-то мешает — напиши /support."
                )
            elif missed >= 3:
                text = (
                    "🤝 *Проверка связи*\n\n"
                    "Три дня без чек-ина. Возможно, марафон идёт в фоне, "
                    "а возможно, нужна помощь.\n\n"
                    "Напиши /support или просто ответь: что мешает?"
                )
            else:
                continue  # missed == 2 — промежуточный, не отправляем

            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
                logger.info(f"[MarathonNudge] Sent to {chat_id} (missed {missed})")
            except Exception as e:
                if _is_user_unavailable(e):
                    await _handle_unavailable_user(chat_id, "marathon nudge")
                else:
                    logger.warning(f"[MarathonNudge] Failed to send to {chat_id}: {e}")

        # WP-330 Ф10.D: перенесено в _send_practice_nudges (запускается каждые 10 мин)
    finally:
        await bot.session.close()


async def _process_marathon_activity_batch():
    """WP-253: ночной batch агрегации календарной активности в marathon_activity.

    Peer-session 2026-06-03-16: агрегирует marathon_state за предыдущий
    календарный день (00:00–23:59 MSK) и upsert'ит в learning.marathon_activity.
    Запускается ежедневно в 03:00 MSK.
    """
    from db.queries.marathon_newcomer import save_marathon_activity
    from db.connection import get_learning_pool

    yesterday = (moscow_now() - timedelta(days=1)).date()
    pool = await get_learning_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                '''SELECT user_id,
                       (check_in_at AT TIME ZONE 'Europe/Moscow')::DATE AS activity_date,
                       COUNT(*)::INTEGER AS raw_count
                 FROM learning.marathon_state
                 WHERE check_in_at >= $1::timestamptz
                   AND check_in_at < $2::timestamptz
                 GROUP BY user_id, (check_in_at AT TIME ZONE 'Europe/Moscow')::DATE''',
                datetime.combine(yesterday, datetime.min.time(), tzinfo=MOSCOW_TZ),
                datetime.combine(moscow_now().date(), datetime.min.time(), tzinfo=MOSCOW_TZ),
            )
        for row in rows:
            await save_marathon_activity(
                user_id=row["user_id"],
                activity_date=row["activity_date"],
                action_type="checkin",
                raw_count=row["raw_count"],
            )
        if rows:
            logger.info(f"[MarathonActivityBatch] Upserted {len(rows)} rows for {yesterday}")
        else:
            logger.info(f"[MarathonActivityBatch] No activity for {yesterday}")
    except Exception as e:
        logger.exception(f"[MarathonActivityBatch] Failed for {yesterday}: {e}")


async def _send_practice_nudges():
    """WP-330 Ф10.D v2: напоминания о практике — через +30 мин и +150 мин после доставки урока.

    Запускается каждые 10 мин. Для каждого пользователя — максимум 2 нуджа в день:
    - Первый  (+30 мин): ключ ...:30m  — в окне 30–150 мин после sent_at
    - Второй  (+150 мин): ключ ...:150m — через 150+ мин после sent_at

    Условия отправки: урок доставлен сегодня, практика не открыта, чек-ин не сделан.
    """
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from db.queries.marathon_newcomer import get_users_for_practice_nudge
    from db.queries.notifications import try_insert_notification

    if not _bot_token:
        return

    practice_users = await get_users_for_practice_nudge()
    if not practice_users:
        return

    bot = Bot(token=_bot_token)
    now = moscow_now()
    try:
        for pu in practice_users:
            chat_id = pu['user_id']
            day = pu['day_number']
            sent_at = pu['sent_at']
            minutes_elapsed = (now - sent_at).total_seconds() / 60

            if minutes_elapsed >= 150:
                nudge_slot = '150m'
                text = (
                    f"⏰ Урок Дня {day} пришёл несколько часов назад, практику ещё не открыли.\n\n"
                    "Последний шанс сегодня — нажмите кнопку ниже:"
                )
            else:
                nudge_slot = '30m'
                text = (
                    f"📚 Урок Дня {day} уже ждёт вас. Осталось только перейти к практике!\n\n"
                    "Нажмите кнопку ниже:"
                )

            nudge_key = f"marathon_practice_nudge:{chat_id}:{day}:{nudge_slot}"
            if not await try_insert_notification(chat_id, 'marathon_practice_nudge', nudge_key):
                continue

            try:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="✏️ Перейти к практике",
                        callback_data=f"marathon_practice:{day}"
                    )
                ]])
                await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)
                logger.info(f"[PracticeNudge] Sent {nudge_slot} nudge to {chat_id} day {day}")
            except Exception as e:
                if _is_user_unavailable(e):
                    await _handle_unavailable_user(chat_id, f"practice nudge {nudge_slot}")
                else:
                    logger.warning(f"[PracticeNudge] Failed to send to {chat_id}: {e}")
    finally:
        await bot.session.close()


async def _send_marathon_weekly_digest():
    """WP-330 P2: еженедельный digest для активных участников (воскресенье 18:00).

    Деdup через notification_log: один digest в неделю (§10.10).
    """
    from db.queries.marathon_newcomer import get_active_marathon_users, get_checkins
    from db.queries.notifications import try_insert_notification

    if not _bot_token:
        return

    users = await get_active_marathon_users()
    if not users:
        return

    bot = Bot(token=_bot_token)
    now = moscow_now()
    week_str = now.strftime('%Y-W%W')
    try:
        for user in users:
            chat_id = user['user_id']
            current_day = user['current_day']
            total_checkins = user['total_checkins']

            # Один digest в неделю на участника (§10.10 dedup)
            digest_key = f"marathon_digest:{chat_id}:{week_str}"
            if not await try_insert_notification(chat_id, 'marathon_digest', digest_key):
                continue

            checkins = await get_checkins(chat_id)
            if checkins:
                last_state = checkins[-1]['state']
                state_labels = {
                    'chaos': '😵 Хаос',
                    'stuck': '🧱 Тупик',
                    'turn': '🔁 Поворот',
                }
                last_state_label = state_labels.get(last_state, last_state)
            else:
                last_state_label = "ещё нет"

            missed = max(0, current_day - total_checkins)

            if current_day == 0:
                logger.info(f"[MarathonDigest] Skipping {chat_id}: not started yet (current_day=0)")
                continue

            text = (
                f"📊 *Итоги недели марафона*\n\n"
                f"📅 День марафона: {current_day} / 14\n"
                f"🌙 Чек-инов: {total_checkins}\n"
                f"❌ Пропущено чек-инов: {missed}\n"
                f"🎯 Последнее состояние: {last_state_label}\n\n"
            )

            if missed == 0 and current_day > 0:
                text += "Отличная неделя! Ритм выдержан. 💪"
            elif missed <= 2:
                text += "Неплохо, но можно добавить стабильности. Продолжаем?"
            else:
                text += "Было сложно, но ты всё ещё в марафоне. Важно — не останавливаться."

            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
                logger.info(f"[MarathonDigest] Sent to {chat_id}")
            except Exception as e:
                if _is_user_unavailable(e):
                    await _handle_unavailable_user(chat_id, "marathon digest")
                else:
                    logger.warning(f"[MarathonDigest] Failed to send to {chat_id}: {e}")
    finally:
        await bot.session.close()


async def _check_marathon_split_delivery():
    """WP-330 С5 watchdog: убедиться что split-формат уроков уехал утром 31 мая.

    Запускается каждые 30 мин в окне 04:00–06:30 МСК 31 мая 2026.
    Если за последние 60 мин 0 lesson_practice-доставок с content_text IS NULL
    (=split-путь) — алерт в MENTOR_CHANNEL_ID.

    Read-only для user state (§10.10b). Dedup per-window через notification_log
    (§10.10). Auto-noop после окна — функция остаётся в коде до W22-close, потом
    удаляется отдельным коммитом.
    """
    from db.queries.notifications import try_insert_notification
    from db.connection import get_learning_pool

    if not MENTOR_CHANNEL_ID or not _bot_token:
        _warn_if_no_mentor_channel()
        return

    now = moscow_now()
    if now.date() != _WP330_C5_WATCHDOG_DATE:
        return
    if not (4 <= now.hour <= 6):
        return

    # Кумулятивный счёт с 04:00 МСК сегодня — иначе sliding 60-мин окно
    # даёт false-positive в поздних слотах (06:05/06:35), если все доставки
    # прошли в 04:00–05:00. См. peer-review 2026-05-30-34, High #1.
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                count(*) FILTER (WHERE content_text IS NULL)     AS split,
                count(*) FILTER (WHERE content_text IS NOT NULL) AS legacy
              FROM learning.marathon_queue
             WHERE status = 'sent'
               AND content_type = 'lesson_practice'
               AND sent_at >= DATE_TRUNC('day', NOW() AT TIME ZONE 'Europe/Moscow')
                            + INTERVAL '4 hours'
        """)
    split = int(row['split'] or 0)
    legacy = int(row['legacy'] or 0)

    window_key = f"marathon_split_watchdog:{now.strftime('%Y-%m-%d-%H%M')}"
    logger.info(f"[MarathonSplitWatchdog] window={window_key} split={split} legacy={legacy}")

    if split > 0:
        return

    if not await try_insert_notification(MENTOR_CHANNEL_ID, 'marathon_split_watchdog', window_key):
        return

    bot = Bot(token=_bot_token)
    try:
        await bot.send_message(
            MENTOR_CHANNEL_ID,
            f"🚨 *WP-330 С5 watchdog*\n\n"
            f"Окно `{now.strftime('%H:%M')}` МСК 31 мая: с 04:00 МСК сегодня 0 split-доставок "
            f"(legacy={legacy}). Migration не сработала или cron спит. Проверь:\n"
            f"• `git log -1 --oneline new-architecture` — С2 (c5820ea) на проде?\n"
            f"• `SELECT count(*) FROM learning.marathon_queue WHERE status='pending' "
            f"AND content_text IS NULL AND content_type='lesson_practice'` ≥ 1?\n"
            f"• Railway logs `[MarathonQueue]` за последний час",
            parse_mode="Markdown",
        )
        logger.warning(f"[MarathonSplitWatchdog] Alert sent: 0 split deliveries in last 60 min (legacy={legacy})")
    finally:
        await bot.session.close()


def _build_marathon_message(content_type: str, day: int, content_ref: str | None, content_text: str | None, intern: dict | None = None) -> str | None:
    """Собрать текст сообщения из кэша или ref.

    WP-330 С9a: intern опционален; если передан — get_day_text применяет routing
    по study_duration/complexity_level и возвращает одну из 4 версий.
    """
    if content_text:
        return content_text
    if content_ref:
        return f"📚 *День {day}*\n\n[Открыть материал]({content_ref})"
    # WP-330 Ф2.6: читаем из marathon-content.json
    # WP-330 Ф10.B + С9a: routing по профилю → long_complex/short_simple/etc.
    from core.marathon_content import get_day_text
    templates = {
        'lesson_practice': f"📚 *День {day}*\n\nСегодняшний урок и практика готовы!",
        'checkin': f"🌙 *День {day} — Вечерний чек-ин*\n\nКак прошёл день? Нажми 😵 / 🧱 / 🔁",
    }
    if content_type == 'lesson_practice':
        lesson = (
            get_day_text(day, 'lesson', intern=intern)
            or get_day_text(day, 'lesson')
        )
        practice = (
            get_day_text(day, 'practice', intern=intern)
            or get_day_text(day, 'practice')
        )
        if lesson and practice:
            message = f"{lesson}\n\n{practice}"
            faq = get_day_text(day, 'faq_hint')
            if faq:
                message += f"\n\n{faq}"
            return message
        return lesson or practice or templates['lesson_practice']
    text = get_day_text(day, content_type)
    if text:
        return text
    return templates.get(content_type)


# --- Blocked user detection ---

_USER_UNAVAILABLE_PHRASES = (
    'blocked', 'deactivated', 'chat not found', 'forbidden',
    'user is deactivated', 'have no rights', 'bot was kicked',
    'not enough rights', 'chat_not_found', 'bot_blocked',
)


def _is_user_unavailable(error: Exception) -> bool:
    """Check if Telegram error indicates user is permanently unreachable."""
    error_msg = str(error).lower()
    return any(phrase in error_msg for phrase in _USER_UNAVAILABLE_PHRASES)


async def _handle_unavailable_user(chat_id: int, context: str = ""):
    """Mark user as blocked and log."""
    from db.queries.users import mark_bot_blocked
    await mark_bot_blocked(chat_id)
    logger.warning(f"[Scheduler] User {chat_id} unavailable ({context}), marked as blocked")


async def _recheck_blocked_users():
    """Проверить заблокированных пользователей с истёкшим next_retry.
    
    BFS2: Без probe-сообщений — только проверяем, не написал ли пользователь
    сам (last_active_date изменился). Если написал — разблокируем.
    """
    from db.queries.users import get_users_to_recheck
    from db.queries.users import get_intern
    from db.queries.users import clear_bot_blocked

    chat_ids = await get_users_to_recheck()
    if not chat_ids:
        return

    logger.info(f"[BlockedUser] Rechecking {len(chat_ids)} blocked users")
    cleared = 0
    for chat_id in chat_ids:
        try:
            intern = await get_intern(chat_id)
            # Если last_active_date обновился после bot_blocked_at — пользователь снова активен
            blocked_at = intern.get("bot_blocked_at")
            last_active = intern.get("last_active_date")
            if blocked_at and last_active and last_active > blocked_at.date():
                await clear_bot_blocked(chat_id)
                logger.info(f"[BlockedUser] Auto-cleared block for {chat_id} (new activity {last_active})")
                cleared += 1
            else:
                # Продлеваем recheck на +1 день
                from db.connection import get_pool
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE development.user_state
                           SET bot_recheck_at = (NOW() AT TIME ZONE 'utc') + interval '1 day'
                           WHERE chat_id = $1""",
                        chat_id,
                    )
        except Exception as e:
            logger.error(f"[BlockedUser] Recheck failed for {chat_id}: {e}")

    logger.info(f"[BlockedUser] Recheck complete: {cleared}/{len(chat_ids)} cleared")


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

    global _scheduler, _bot_dispatcher, _aiogram_dispatcher, _bot_token, _bot_id
    _bot_dispatcher = bot_dispatcher
    _aiogram_dispatcher = aiogram_dispatcher
    _bot_token = bot_token
    _bot_id = int(bot_token.split(':')[0])

    _scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    _scheduler.add_job(scheduled_check, 'cron', minute='*', max_instances=2)
    _scheduler.add_job(pre_generate_upcoming, 'cron', minute='*', max_instances=2)  # Pre-gen за 3ч до доставки
    _scheduler.add_job(_neon_keep_alive, 'cron', minute='*/4')  # Keep-alive каждые 4 мин
    _scheduler.add_job(_better_stack_heartbeat, 'cron', minute='*')  # WP-244: heartbeat ping каждую минуту
    _scheduler.add_job(_discourse_scheduled_publish, 'cron', minute='7,37')  # Discourse: scheduled posts (offset from :00/:30)
    _scheduler.add_job(_discourse_check_comments, 'cron', minute='3')  # Discourse: comment polling (1x/hour, was 4x — rate limit 429)
    _scheduler.add_job(_smart_publisher_scan, 'cron', hour=5, minute=7)  # Publisher: daily scan 05:07 MSK (after strategist ~04:00)
    # Startup scan: компенсация пропущенного cron при редеплое после 05:07 MSK (cooldown предотвращает дубли)
    _scheduler.add_job(_smart_publisher_scan, 'date', run_date=datetime.now(MOSCOW_TZ) + timedelta(minutes=2), id='publisher_startup_scan', kwargs={'notify': False})
    _scheduler.add_job(_send_slot_daily_prompt, 'cron', hour=19, minute=0)  # WP-310 Ф13c: slot prompt 22:00 МСК (= 19:00 UTC)
    _scheduler.add_job(_ensure_reminder_text_column, 'date', run_date=datetime.now(MOSCOW_TZ) + timedelta(seconds=10), id='ensure_reminder_text')
    _scheduler.add_job(_gateway_proactive_refresh, 'cron', minute='*/10')  # Gateway: Ory token refresh every 10 min (WP-209, covers DT too)
    _scheduler.add_job(_process_marathon_queue, 'cron', minute='*/10')  # WP-330: новичок-марафон очередь
    _scheduler.add_job(_send_practice_nudges, 'cron', minute='*/10')  # WP-330 Ф10.D: нуджи +30/+150 мин после доставки
    _scheduler.add_job(_process_marathon_activity_batch, 'cron', hour=3, minute=0)  # WP-253: nightly activity aggregation
    _scheduler.add_job(_check_marathon_missed_checkins, 'cron', hour='*/6')  # WP-330 P1: алерты наставникам о пропусках
    _scheduler.add_job(_send_marathon_nudges, 'cron', hour=10, minute=0)  # WP-330 P2: nudge при пропуске
    _scheduler.add_job(_send_marathon_weekly_digest, 'cron', day_of_week='sun', hour=18, minute=0)  # WP-330 P2: digest вс 18:00
    _scheduler.add_job(_check_marathon_split_delivery, 'cron', hour='4,5,6', minute='5,35', id='marathon_split_watchdog', max_instances=1)  # WP-330 С5: split rollout watchdog (auto-noop вне 31.05 04:00-06:30 МСК)
    _scheduler.add_job(_recheck_blocked_users, 'cron', hour=6, minute=0)  # BFS2: recheck blocked users daily 06:00
    _scheduler.add_job(_rollback_expired_burn_reservations, 'cron', minute='*/5')  # WP-327: откат «зависших» резервов баллов (>30 мин)

    _scheduler.add_job(_discourse_typing_collect, 'cron', hour=3, minute=30)   # WP-327 Phase 3б: Discourse typing collection 03:30 UTC
    _scheduler.add_job(_discourse_typing_collect, 'cron', hour=17, minute=0)  # WP-327 Phase 3б: второй запуск 20:00 МСК
    _scheduler.add_job(_wakatime_typing_collect, 'cron', hour=22, minute=0)   # WP-327 Phase 4: WakaTime typing 22:00 UTC
    _scheduler.add_job(_refresh_subscribers_snapshot, 'cron', hour=1, minute=0)  # WP-327 Этап 13: subscribers snapshot 01:00 UTC (04:00 МСК)
    _scheduler.add_job(_claude_health_probe, 'interval', minutes=5, id='claude_health_probe', max_instances=1)  # WP-7: canary probe
    _scheduler.add_job(_check_retry_storm, 'interval', minutes=5, id='check_retry_storm', max_instances=1)  # BE5: retry storm detector (id без retry_-префикса — иначе детектор считает себя в storm:1)
    # WP-268 Phase 4+: _dt_sync_engagement отключён — читает development.* views из старого aist_bot Neon
    # (development.engagement, development.user_events), которых нет в Railway Postgres bot_data.
    # Новая архитектура: projection-worker (WP-270) → indicators.calculated_profile (Neon).
    # _scheduler.add_job(_dt_sync_engagement, 'cron', hour=4, minute=30)
    _scheduler.start()

    # One-time cleanup: обнулить question_content с текстом ошибки (bug fix)
    _scheduler.add_job(cleanup_error_questions, 'date', run_date=datetime.now(MOSCOW_TZ) + timedelta(seconds=30), id='cleanup_error_questions')

    # WP-253 Gap C: one-time notification to users needing GitHub relink (10 min after start)
    _scheduler.add_job(_notify_github_relink, 'date', run_date=datetime.now(MOSCOW_TZ) + timedelta(minutes=10), id='github_relink_notification')

    # WP-327 Этап 13: startup fallback — заполнить snapshot для сегодня если бот стартовал после полуночи до 01:00 UTC
    _scheduler.add_job(_refresh_subscribers_snapshot, 'date', run_date=datetime.now(MOSCOW_TZ) + timedelta(seconds=60), id='subscribers_snapshot_startup')

    logger.info("[Scheduler] Планировщик инициализирован (+ Neon keep-alive + pre-gen + Discourse + publisher startup scan)")
    return _scheduler


PREGEN_HOURS_AHEAD = 3


async def _generate_and_save_content(chat_id: int, intern: dict, topic_index: int) -> bool:
    """Сгенерировать урок+вопрос+практику и сохранить в marathon_content.

    Извлечённая логика из send_scheduled_topic() — используется и для пре-генерации,
    и как fallback при доставке.

    Returns:
        True если урок успешно сгенерирован и сохранён.
    """
    from clients import claude
    from core.topics import get_topic, get_topics_for_day, TOPICS

    topic = get_topic(topic_index)
    if not topic:
        return False

    bloom_level = intern.get('complexity_level', 1) or intern.get('bloom_level', 1) or 1

    # Генерируем все 3 типа параллельно (gateway_mcp используется внутри generate_content)
    lesson_task = claude.generate_content(
        topic=topic, intern=intern
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

    # Валидация длины: пропорциональная проверка по Content Budget Model
    # calc_words даёт целевые слова, ×5 ≈ символы, порог 30% от ожидаемого
    from config import calc_words
    study_dur = intern.get('study_duration', 15)
    expected_chars = calc_words(study_dur, bloom_level) * 5
    min_chars = max(200, int(expected_chars * 0.3))

    if lesson_content is not None and len(lesson_content) < min_chars:
        logger.error(
            f"[PreGen] Lesson too short ({len(lesson_content)} chars, "
            f"min {min_chars}, expected ~{expected_chars}) for {chat_id}, "
            f"topic {topic_index} — likely partial or error fallback"
        )
        lesson_content = None

    if isinstance(results[1], Exception):
        logger.warning(f"[PreGen] Question generation failed for {chat_id}: {results[1]}")
    if isinstance(results[2], Exception):
        logger.warning(f"[PreGen] Practice generation failed for {chat_id}: {results[2]}")

    # Сохраняем всё что удалось (вопрос/практика сохраняются даже без урока)
    has_any = lesson_content or question_content or practice_content
    if not has_any:
        return False

    await save_marathon_content(
        chat_id=chat_id,
        topic_index=topic_index,
        lesson_content=lesson_content,
        question_content=question_content,
        practice_content=practice_content,
        bloom_level=bloom_level,
    )

    if lesson_content is None:
        logger.warning(f"[PreGen] Lesson failed but saved question/practice for {chat_id}, topic {topic_index}")
        return False  # Lesson still missing — pre-gen incomplete

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
                        claude.generate_content(topic=pair_topic, intern=intern),
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
    if is_api_degraded():
        logger.info(f"[LookAhead] Claude API degraded, skip look-ahead for {chat_id}")
        return

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
            if is_api_degraded():
                logger.info(f"[PreGen] Claude API degraded, skip pre-gen for {chat_id}")
                return
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
                _schedule_retry(chat_id, 'marathon')

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

        if not content or not content.get('main_content'):
            logger.error(f"[Scheduler] Feed: digest generation returned empty for {chat_id}")
            _schedule_retry(chat_id, 'feed')
            return

        # Сохраняем как pending (не показана пользователю)
        topics_title = ", ".join(topics)
        session = await create_feed_session(
            week_id=week['id'],
            day_number=depth_level,
            topic_title=topics_title,
            content=content,
            session_date=today,
            status='pending',
        )
        if not session:
            # UPSERT returned None → active/completed session already exists (race condition guard)
            logger.info(f"[Scheduler] Feed: session already exists for {chat_id} today (UPSERT skip)")
            return
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

    # Перепроверяем перед отправкой: другой поток мог уже отправить (race guard)
    recheck = await get_feed_session(week['id'], today)
    if recheck and recheck.get('status') != 'pending':
        logger.info(f"[Scheduler] Feed: {chat_id} — session already active/completed before notification, skip")
        return

    # WP-152: idempotency через notification_log
    from db.queries.notifications import try_insert_notification
    today_str = today.strftime('%Y-%m-%d')
    feed_key = f"feed_digest:{chat_id}:{today_str}"
    if not await try_insert_notification(chat_id, 'feed_digest', feed_key):
        logger.info(f"[Scheduler] Feed digest already sent to {chat_id} today, skip")
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
        if _is_user_unavailable(e):
            await _handle_unavailable_user(chat_id, "feed notification")
        else:
            logger.error(f"[Scheduler] Error sending feed notification to {chat_id}: {e}")


async def send_scheduled_topic(chat_id: int, bot: Bot):
    """Отправка уведомления о готовности урока марафона по расписанию.

    Проверяет, пре-генерирован ли контент (за 3ч через pre_generate_upcoming).
    Если да — сразу уведомление. Если нет — fallback на генерацию сейчас.
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from core.topics import get_marathon_day, get_next_topic_index, get_topic, get_total_topics, get_lessons_tasks_progress
    from core.knowledge import get_topic_title

    if is_api_degraded():
        logger.warning(f"[Scheduler] Claude API degraded, deferring scheduled topic for {chat_id}")
        return True

    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    # Guard: пользователи нового движка (WP-330) получают уроки через _process_marathon_queue.
    # Устраняет race condition, при которой legacy scheduler дублировал уроки
    # пользователям с learning.marathon_progress (например, Дарья @dnbutorina).
    if await is_on_newcomer_marathon(chat_id):
        logger.info(f"[Scheduler] {chat_id}: newcomer marathon user, skip legacy send_scheduled_topic")
        return

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
            # Марафон пройден — C1 конверсия в программы (DP.ARCH.002 § 12)
            from config.settings import PLATFORM_URLS

            progress = get_lessons_tasks_progress(intern['completed_topics'])

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('marathon.btn_program_lr', lang),
                    url=PLATFORM_URLS['lr'],
                )],
                [InlineKeyboardButton(
                    text=t('marathon.btn_continue_feed', lang),
                    callback_data="mode_feed",
                )],
            ])

            # Завершаем марафон ПЕРВЫМ — исключаем из scheduler ДО отправки сообщения.
            # Иначе: send_message OK → update_intern fail → catch-up через 30 мин
            # повторно находит user (status=active, нет marathon_content) → дубль поздравления.
            feed_status = intern.get('feed_status', 'not_started')
            new_mode = derive_mode(MarathonStatus.COMPLETED, feed_status)
            await update_intern(chat_id, marathon_status=MarathonStatus.COMPLETED, mode=new_mode)
            logger.info(f"[Scheduler] {chat_id}: marathon completed, status → completed, mode → {new_mode}")

            # WP-151 Ф3: marathon_completed
            from db.queries.events import log_event
            await log_event(chat_id, 'marathon_completed', {
                'total_topics': total,
                'completed_topics': completed_count,
                'path': 'scheduler',
            })

            try:
                await bot.send_message(
                    chat_id,
                    f"🎉 *{t('marathon.congratulations_completed', lang)}*\n\n"
                    f"{t('marathon.completed_all_days', lang, days=MARATHON_DAYS, topics=total)}\n\n"
                    f"📊 *{t('marathon.your_statistics', lang)}:*\n"
                    f"📖 {t('progress.lessons', lang)}: {progress['lessons']['completed']}/{progress['lessons']['total']}\n"
                    f"📝 {t('progress.tasks', lang)}: {progress['tasks']['completed']}/{progress['tasks']['total']}\n\n"
                    f"{t('marathon.completed_next_step', lang)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.error(f"[Scheduler] {chat_id}: marathon completed but send_message failed: {e}")
        return

    # НЕ обновляем current_topic_index здесь — scheduler только пре-генерирует контент.
    # current_topic_index обновляется в lesson.py при реальном взаимодействии пользователя.

    # ─── Idempotency early guard: уведомление уже отправлено сегодня? (WP-152: notification_log) ───
    from db.queries.notifications import was_notification_sent
    today_str = moscow_now().strftime('%Y-%m-%d')
    marathon_key = f"marathon_lesson:{chat_id}:{today_str}:topic{topic_index}"
    if await was_notification_sent(marathon_key):
        logger.info(f"[Scheduler] {chat_id}: notification already sent today for topic {topic_index}, skip (early guard)")
        return

    # Legacy fallback: проверяем старый guard на переходный период
    existing = await get_marathon_content(chat_id, topic_index)
    if existing and existing.get('notification_sent_at'):
        sent_date = existing['notification_sent_at'].date()
        today = moscow_now().date()
        if sent_date >= today:
            logger.info(f"[Scheduler] {chat_id}: notification already sent today for topic {topic_index}, skip (legacy guard)")
            return

    # ─── Проверяем: контент уже пре-генерирован (за 3h)? ───
    if existing and existing.get('status') == 'pending' and existing.get('lesson_content'):
        logger.info(f"[Scheduler] Pre-generated content found for {chat_id}, topic {topic_index} — skip generation")
    else:
        # Fallback: генерируем сейчас (контент не был пре-генерирован)
        if is_api_degraded():
            logger.warning(f"[Scheduler] API degraded, skip on-demand gen for {chat_id}, topic {topic_index}")
            return True
        try:
            success = await _generate_and_save_content(chat_id, intern, topic_index)
            if not success:
                logger.error(f"[Scheduler] Lesson generation failed for {chat_id}, topic {topic_index}")
                return True
            logger.info(f"[Scheduler] On-demand generation for {chat_id}, topic {topic_index}")
        except asyncio.TimeoutError:
            logger.error(f"[Scheduler] Pre-generation timeout (120s) for {chat_id}, topic {topic_index}")
            return True
        except Exception as e:
            logger.error(f"[Scheduler] Pre-generation error for {chat_id}: {e}")
            return True

    # ─── Idempotency guard: уведомление уже отправлено сегодня? (WP-152: notification_log) ───
    if await was_notification_sent(marathon_key):
        logger.info(f"[Scheduler] {chat_id}: notification already sent today for topic {topic_index}, skip")
        return

    # Планируем напоминания (+1ч и +3ч) — legacy таблица reminders, пока не мигрировано
    await schedule_reminders(chat_id, intern)

    # Определяем: catch-up (урок с прошлого дня) или обычный
    is_catchup = topic['day'] < marathon_day

    # Log-before-send: записываем факт отправки ДО send_message (§10.10, WP-152)
    from db.queries.notifications import try_insert_notification
    await try_insert_notification(chat_id, 'marathon_lesson', marathon_key)
    await mark_notification_sent(chat_id, topic_index)  # legacy: двойная запись на переходный период

    # Отправляем уведомление с кнопкой «Получить урок»
    topic_title = get_topic_title(topic, lang)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📚 {t('buttons.get_lesson', lang)}",
            callback_data="marathon_get_lesson"
        )]
    ])

    if is_catchup:
        await bot.send_message(
            chat_id,
            f"*{t('reminders.marathon_catchup_ready', lang)}*\n"
            f"📚 {topic_title}\n\n"
            f"{t('reminders.marathon_catchup_cta', lang)}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        logger.info(f"[Scheduler] Sent CATCH-UP notification to {chat_id}, topic: {topic_title} (day {topic['day']} < marathon_day {marathon_day})")
    else:
        await bot.send_message(
            chat_id,
            f"*{t('reminders.marathon_lesson_ready', lang)}*\n"
            f"📚 {topic_title}\n\n"
            f"{t('reminders.marathon_lesson_cta', lang)}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        logger.info(f"[Scheduler] Sent marathon notification to {chat_id}, topic: {topic_title}")


async def _check_retry_storm():
    """BE5: детектор retry-шторма — >10 retry jobs в очереди → TG-алерт."""
    if not _scheduler:
        return
    retry_jobs = [j for j in _scheduler.get_jobs() if j.id.startswith('retry_')]
    if len(retry_jobs) <= 10:
        return
    by_type = Counter(j.id.split('_')[1] for j in retry_jobs if '_' in j.id)
    lines = [f"  {k}: {v}" for k, v in by_type.items()]
    alert = (
        f"⚠️ <b>[Scheduler] Retry storm:</b> {len(retry_jobs)} jobs в очереди\n"
        + "\n".join(lines)
    )
    import os
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    if not dev_chat_id or not _bot_token:
        logger.warning(f"[Scheduler] Retry storm detected ({len(retry_jobs)} jobs) but no dev_chat_id/token")
        return
    try:
        bot = Bot(token=_bot_token)
        await bot.send_message(int(dev_chat_id), alert, parse_mode="HTML")
        await bot.session.close()
    except Exception as e:
        logger.error(f"[Scheduler] Retry storm alert failed: {e}")
    logger.warning(f"[Scheduler] Retry storm: {len(retry_jobs)} retry jobs — {dict(by_type)}")


async def _ensure_reminder_text_column():
    """WP-320: добавить text столбец в reminder, если ещё нет.
    bot_id больше не ensure-им здесь — его гарантирует миграция 024 (WP-212 Layer 1)."""
    try:
        from db.connection import get_learning_pool
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE reminder ADD COLUMN IF NOT EXISTS text TEXT"
            )
        logger.info("[Scheduler] reminder.text column ensured")
    except Exception as e:
        logger.warning("[Scheduler] _ensure_reminder_text_column failed: %s", e)


async def _get_blocked_chat_ids() -> set[int]:
    """WP-253 lift-and-shift: получить список заблокированных пользователей.

    После lift-and-shift таблица reminder в learning БД, user_state — в bot_data.
    Cross-DB JOIN невозможен → отдельный запрос к user_state pool.
    """
    from db.connection import get_pool as _get_user_state_pool
    user_state_pool = await _get_user_state_pool()
    async with user_state_pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT chat_id FROM development.user_state WHERE bot_blocked IS TRUE'
        )
    return {r['chat_id'] for r in rows}


async def schedule_reminders(chat_id: int, intern: dict):
    """Планирует напоминания для пользователя.

    WP-253 lift-and-shift: таблица reminders → learning.reminder.
    """
    from db.connection import get_learning_pool

    now = moscow_now()

    async with (await get_learning_pool()).acquire() as conn:
        # Удаляем старые неотправленные напоминания
        await conn.execute(
            'DELETE FROM reminder WHERE chat_id = $1 AND sent = FALSE',
            chat_id
        )

        # Планируем напоминания +1ч и +3ч
        for hours in [1, 3]:
            reminder_time = now + timedelta(hours=hours)
            # Убираем timezone для совместимости с TIMESTAMP (без timezone)
            reminder_time_naive = reminder_time.replace(tzinfo=None)
            await conn.execute(
                '''INSERT INTO reminder (chat_id, reminder_type, scheduled_for, bot_id)
                   VALUES ($1, $2, $3, $4)''',
                chat_id, f'+{hours}h', reminder_time_naive, _bot_id
            )


async def send_user_reminder(chat_id: int, text: str, reminder_id: int, bot: Bot):
    """WP-320 Ф2: доставка пользователь-инициированного напоминания (DP.SC.134).
    see DP.SC.134, DP.ROLE.044
    """
    from db.queries.notifications import try_insert_notification
    from db.queries.events import log_event

    idempotency_key = f"user_remind:{chat_id}:{reminder_id}"
    # Idempotency best-effort: domain_event недоступен → не блокирует отправку.
    # reminder.sent=TRUE уже выставлен до этого вызова — защита от дублей.
    try:
        inserted = await try_insert_notification(chat_id, 'reminder', idempotency_key)
        if not inserted:
            logger.info("[Scheduler] user_reminder %s already sent to %s, skip", reminder_id, chat_id)
            return
    except Exception as e:
        logger.warning("[Scheduler] idempotency check failed for reminder %s: %s — proceeding", reminder_id, e)

    try:
        # parse_mode="Markdown" → SafeBot перехватывает и конвертирует **жирный**/_курсив_
        # в HTML (см. core/safe_bot.py). Без него GitHub-разметка (**) приходит в TG литералом.
        await bot.send_message(chat_id, f"🔔 {text}", parse_mode="Markdown")
        logger.info("[Scheduler] user_reminder %s delivered to %s", reminder_id, chat_id)
    except Exception:
        logger.exception("[Scheduler] user_reminder %s failed for %s", reminder_id, chat_id)
        raise

    # Non-critical logging — не бросает исключение (log_event уже fault-tolerant)
    await log_event(chat_id, 'reminder_delivered', {'reminder_type': 'custom', 'reminder_id': reminder_id})


async def send_reminder(chat_id: int, reminder_type: str, bot: Bot):
    """Отправляет напоминание с кнопкой «Получить урок»."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from core.topics import get_marathon_day, get_display_day

    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    # WP-330 cutover (Block MAR): пользователь переведён на новый движок марафона
    # (есть строка в learning.marathon_progress) → legacy +1h/+3h напоминания для
    # него отключены. Симметрично гейту доставки в get_all_scheduled_interns.
    # Без этого: старые/мигрированные напоминания «День N не начат» летят параллельно
    # новой доставке (два счётчика дня → «День 1 не начат» при пройденном «Дне 2»).
    # Строка reminder уже помечена sent=TRUE в check_reminders → повтора не будет.
    from db.queries.marathon_newcomer import is_on_newcomer_marathon
    if await is_on_newcomer_marathon(chat_id):
        logger.info(f"[Scheduler] {chat_id}: на новом движке марафона, гашу legacy-напоминание {reminder_type}")
        return

    topics_today = get_topics_today(intern)

    # Если уже начал изучение сегодня — не напоминаем
    if topics_today > 0:
        return

    marathon_day = get_marathon_day(intern)
    if marathon_day == 0:
        return
    display_day = get_display_day(intern)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📚 {t('buttons.get_lesson', lang)}",
            callback_data="marathon_get_lesson"
        )]
    ])

    # WP-152: двойная запись в notification_log
    from db.queries.notifications import try_insert_notification
    today_str = moscow_now().strftime('%Y-%m-%d')
    reminder_key = f"reminder:{chat_id}:{today_str}:{reminder_type}"
    inserted = await try_insert_notification(chat_id, 'reminder', reminder_key)
    if not inserted:
        logger.info(f"[Scheduler] Reminder {reminder_type} already sent to {chat_id} today, skip")
        return

    # WP-151 Ф3: reminder_delivered
    from db.queries.events import log_event
    await log_event(chat_id, 'reminder_delivered', {
        'reminder_type': reminder_type,
        'marathon_day': marathon_day,
    })

    if reminder_type == '+1h':
        await bot.send_message(
            chat_id,
            f"⏰ *{t('reminders.title', lang)}*\n\n"
            f"{t('reminders.day_waiting', lang, day=display_day)}\n\n"
            f"{t('reminders.two_topics_today', lang)}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    elif reminder_type == '+3h':
        await bot.send_message(
            chat_id,
            f"🔔 *{t('reminders.last_reminder', lang)}*\n\n"
            f"{t('reminders.day_not_started', lang, day=display_day)}\n\n"
            f"{t('reminders.regularity_tip', lang)}\n"
            f"{t('reminders.even_15_min', lang)}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )


async def check_reminders():
    """Проверяет и отправляет запланированные напоминания.

    Использует SELECT FOR UPDATE SKIP LOCKED для предотвращения дублей:
    если предыдущий scheduled_check ещё обрабатывает напоминания,
    следующий цикл (через 1 мин) пропустит уже залоченные строки.

    WP-253 lift-and-shift: таблица reminder в learning БД, user_state — в bot_data.
    Cross-DB JOIN невозможен → pre-fetch blocked chat_ids отдельным запросом
    и фильтруем через ANY(...) на уровне learning pool.
    """
    from db.connection import get_learning_pool

    # scheduled_for is stored as UTC naive — compare with UTC, not Moscow time.
    now_naive = datetime.utcnow()

    # Pre-fetch заблокированных пользователей из user_state pool (bot_data).
    try:
        blocked_ids = await _get_blocked_chat_ids()
    except Exception as e:
        logger.warning("[Scheduler] _get_blocked_chat_ids failed: %s — skipping block filter", e)
        blocked_ids = set()
    blocked_list = list(blocked_ids) if blocked_ids else [0]  # NULL-safe placeholder

    pool = await get_learning_pool()
    bot_data_pool = await get_pool()
    bot = Bot(token=_bot_token)

    logger.debug("[Scheduler] check_reminders: now_naive=%s, blocked=%d", now_naive, len(blocked_list))

    try:
        async with pool.acquire() as conn:
            # Обрабатываем по одному в транзакции: claim → send → mark sent
            while True:
                row = await conn.fetchrow(
                    '''UPDATE reminder SET sent = TRUE
                       WHERE id = (
                           SELECT r.id FROM reminder r
                           WHERE r.sent = FALSE AND r.scheduled_for <= $1
                             AND NOT (r.chat_id = ANY($2::bigint[]))
                             AND r.bot_id = $3
                           ORDER BY r.scheduled_for
                           LIMIT 1
                           FOR UPDATE OF r SKIP LOCKED
                       )
                       RETURNING id, chat_id, reminder_type, text''',
                    now_naive, blocked_list, _bot_id
                )
                if not row:
                    break

                # P0: TOCTOU guard — re-check bot_blocked right before sending.
                # If blocked: revert sent=TRUE so the reminder isn't silently lost.
                is_blocked_now = await bot_data_pool.fetchval(
                    "SELECT bot_blocked FROM development.user_state WHERE chat_id = $1",
                    row['chat_id']
                )
                if is_blocked_now:
                    await conn.execute("UPDATE reminder SET sent = FALSE WHERE id = $1", row['id'])
                    logger.info(f"[Scheduler] Reverted reminder {row['id']} — user {row['chat_id']} blocked (TOCTOU)")
                    continue

                try:
                    # WP-320 Ф2: custom text reminders (DP.SC.134)
                    if row['reminder_type'] == 'custom' and row.get('text'):
                        await send_user_reminder(row['chat_id'], row['text'], row['id'], bot)
                    else:
                        await send_reminder(row['chat_id'], row['reminder_type'], bot)
                    logger.info(f"Sent {row['reminder_type']} reminder to {row['chat_id']}")
                except Exception as e:
                    if _is_user_unavailable(e):
                        logger.warning(f"User {row['chat_id']} blocked bot, marking reminder {row['id']} as sent")
                        await _handle_unavailable_user(row['chat_id'], f"reminder {row['reminder_type']}")
                    else:
                        # Retry limit: increment fail_count, give up after 3 attempts
                        fail_count = await conn.fetchval(
                            'SELECT COALESCE(fail_count, 0) FROM reminder WHERE id = $1',
                            row['id']
                        )
                        if fail_count is not None and fail_count >= 2:
                            # 3rd failure — give up, leave sent=TRUE
                            logger.error(f"Reminder {row['id']} to {row['chat_id']} failed 3 times, giving up: {e}")
                        else:
                            # Откатываем sent=TRUE и инкрементируем fail_count для retry
                            await conn.execute(
                                'UPDATE reminder SET sent = FALSE, fail_count = COALESCE(fail_count, 0) + 1 WHERE id = $1',
                                row['id']
                            )
                            logger.error(f"Failed to send reminder to {row['chat_id']} (attempt {(fail_count or 0) + 1}/3): {e}")
    finally:
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
            """Обработка одного пользователя (marathon + feed).

            Tailor delivery (WP-149 Портной) удалена 11 мая 2026 (WP-301):
            бот = только Марафон + Лента + ссылки/напоминания. Персональное
            руководство доставляется через git-канал (личный репо пилота +
            GitHub App). См. /lesson + /lesson-close скиллы.
            """
            try:
                if send_type in ('marathon', 'both'):
                    if await send_scheduled_topic(chat_id, bot):
                        intern_chk = await get_intern(chat_id)
                        exhausted = intern_chk.get('retry_exhausted_date') if intern_chk else None
                        if exhausted and exhausted >= moscow_now().date():
                            logger.info(f"[Scheduler] {chat_id}: retry exhausted today, skip new chain")
                        else:
                            _schedule_retry(chat_id, 'marathon', attempt=0)
                if send_type in ('feed', 'both'):
                    await pre_generate_feed_digest(chat_id, bot)
                logger.info(f"[Scheduler] Sent {send_type} to {chat_id}")
            except Exception as e:
                if _is_user_unavailable(e):
                    await _handle_unavailable_user(chat_id, f"scheduled {send_type}")
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

    # 🎯 Milestone notifications (11:00 MSK daily — C3, DP.ARCH.002 § 12.5)
    if now.hour == 11 and now.minute == 0:
        try:
            await send_milestone_notifications()
        except Exception as e:
            logger.error(f"[Scheduler] Milestone notification error: {e}")

    # 📅 Event notifications (12:00 MSK daily — C7, DP.ARCH.002 § 12.7)
    if now.hour == 12 and now.minute == 0:
        try:
            await send_event_notifications()
        except Exception as e:
            logger.error(f"[Scheduler] Event notification error: {e}")

    # 🔔 Engagement nudges (13:00 MSK daily — WP-85 Phase 5C)
    if now.hour == 13 and now.minute == 0:
        try:
            await send_engagement_nudges()
        except Exception as e:
            logger.error(f"[Scheduler] Engagement nudge error: {e}")

    # 🔍 Schedule integrity check: 08:00 MSK daily — detect silent delivery failures
    if now.hour == 8 and now.minute == 0 and dev_chat_id:
        try:
            alert = await _check_schedule_integrity(now)
            if alert:
                bot = Bot(token=_bot_token)
                try:
                    await bot.send_message(int(dev_chat_id), alert, parse_mode="HTML")
                    logger.warning(f"[Scheduler] Schedule integrity alert sent")
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f"[Scheduler] Schedule integrity check error: {e}")

    # 🚨 Latency alert: проверяем каждые 15 минут
    if now.minute % 15 == 0 and dev_chat_id:
        try:
            from db.queries.traces import check_latency_alerts, check_nav_latency_alerts
            # Early-warning: nav-red precedes consultation-red by 2+ hours
            nav_alert = await check_nav_latency_alerts(minutes=15)
            if nav_alert:
                bot = Bot(token=_bot_token)
                try:
                    await bot.send_message(int(dev_chat_id), nav_alert, parse_mode="HTML")
                    logger.info("[Scheduler] Nav latency early-warning sent")
                finally:
                    await bot.session.close()
            # Main latency alert (consultation + nav)
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

    # 🚨 Error alert: проверяем каждые 15 минут (enhanced with classifier, WP-45)
    if now.minute % 15 == 0 and dev_chat_id:
        try:
            from db.queries.errors import check_error_alerts
            alert_text = await check_error_alerts(minutes=15)
            if alert_text:
                bot = Bot(token=_bot_token)
                try:
                    try:
                        await bot.send_message(int(dev_chat_id), alert_text, parse_mode="HTML")
                    except Exception:
                        # Fallback: отправить без HTML если парсинг сломан
                        await bot.send_message(int(dev_chat_id), alert_text)
                    logger.info("[Scheduler] Error alert sent to developer")
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f"[Scheduler] Error alert error: {e}")

    # 🚨 L4 Escalation: L3/L4/unknown ошибки → отдельный алерт (WP-45)
    if now.minute % 15 == 0 and dev_chat_id:
        try:
            from core.error_classifier import check_escalation
            escalation_text = await check_escalation()
            if escalation_text:
                bot = Bot(token=_bot_token)
                try:
                    try:
                        await bot.send_message(int(dev_chat_id), escalation_text, parse_mode="HTML")
                    except Exception:
                        await bot.send_message(int(dev_chat_id), escalation_text)
                    logger.info("[Scheduler] Escalation alert sent to developer")
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f"[Scheduler] Escalation check error: {e}")

    # 🔧 L2 Auto-Fix: detect errors → Claude diagnosis → TG approval (WP-45 Phase 3)
    if now.minute % 15 == 0 and dev_chat_id:
        try:
            from core.autofix import run_autofix_cycle
            bot = Bot(token=_bot_token)
            try:
                proposals = await run_autofix_cycle(bot, dev_chat_id)
                if proposals > 0:
                    logger.info(f"[Scheduler] AutoFix: {proposals} proposals sent")
            finally:
                await bot.session.close()
        except Exception as e:
            logger.error(f"[Scheduler] AutoFix cycle error: {e}")

    # 🚨 L3 Health Check: каскадные ошибки → Railway restart (WP-45 Phase 4)
    if now.minute % 15 == 0 and dev_chat_id:
        try:
            from core.health_check import run_l3_health_check
            bot = Bot(token=_bot_token)
            try:
                restarted = await run_l3_health_check(bot, dev_chat_id)
                if restarted:
                    logger.warning("[Scheduler] L3: Railway restart triggered")
            finally:
                await bot.session.close()
        except Exception as e:
            logger.error(f"[Scheduler] L3 health check error: {e}")

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
            from db.queries.autofix import cleanup_old_fixes
            await cleanup_old_fixes(days=30)
        except Exception as e:
            logger.error(f"[Scheduler] AutoFix cleanup error: {e}")
        try:
            from db.queries.cache import cache_cleanup
            await cache_cleanup()
        except Exception as e:
            logger.error(f"[Scheduler] Cache cleanup error: {e}")
        try:
            from db.queries.oauth_states import cleanup_expired_oauth_states
            await cleanup_expired_oauth_states()
        except Exception as e:
            logger.error(f"[Scheduler] OAuth states cleanup error: {e}")
        try:
            # WP-268 Phase 3 Block 1: fsm_states живёт в FSM_URL (Railway-local PG, паттерн §10.10)
            from db.connection import get_fsm_pool
            pool = await get_fsm_pool()
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM fsm_states WHERE updated_at < NOW() - INTERVAL '30 days'"
                )
                count = int(result.split()[-1]) if result else 0
                if count > 0:
                    logger.info(f"[Scheduler] FSM cleanup: удалено {count} устаревших сессий (>30 дней)")
        except Exception as e:
            logger.error(f"[Scheduler] FSM cleanup error: {e}")

        # Финализация устаревших сессий
        try:
            from db.queries.sessions import finalize_stale_sessions
            await finalize_stale_sessions()
        except Exception as e:
            logger.error(f"[Scheduler] Session cleanup error: {e}")

    # 🔄 Catch-up: пропущенные доставки марафона (каждые 30 мин)
    if now.minute % 30 == 0:
        try:
            await _catch_up_missed_deliveries()
        except Exception as e:
            logger.error(f"[Scheduler] Catch-up delivery error: {e}")

    # 🔧 Unstick: проверяем застрявших пользователей каждые 5 минут
    if now.minute % 5 == 0:
        try:
            from core.unstick import check_and_recover_users
            await check_and_recover_users()
        except Exception as e:
            logger.error(f"[Scheduler] Unstick check error: {e}")

    # 🏷️ Error classifier: классифицируем новые ошибки каждые 5 мин (WP-45)
    if now.minute % 5 == 0:
        try:
            from core.error_classifier import classify_unprocessed
            await classify_unprocessed()
        except Exception as e:
            logger.error(f"[Scheduler] Error classifier error: {e}")

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
# CATCH-UP: ПРОПУЩЕННЫЕ ДОСТАВКИ
# ═══════════════════════════════════════════════════════════

async def _catch_up_missed_deliveries():
    """Компенсирующая доставка: найти пользователей, чьё время уже прошло,
    но уведомление за сегодня не отправлено → перезапустить доставку.

    Запускается каждые 30 минут. Покрывает случаи:
    - Редеплой Railway (потеря cron-джобов)
    - Таймаут Claude API при пре-генерации + исчерпание retry

    Использует notification_sent_at (не created_at) для проверки —
    контент может быть пре-генерирован заранее, но уведомление не отправлено.
    """
    # WP-253 lift-and-shift: marathon_content переехал в learning БД,
    # user_state остался в bot_data (G1 hold-out). Cross-DB JOIN невозможен,
    # split на 2 round-trip.
    from db.connection import get_pool, get_learning_pool

    now = moscow_now()
    time_str = f"{now.hour:02d}:{now.minute:02d}"
    today_msk = now.date()

    # Шаг 1: chat_ids уже получивших уведомление сегодня (learning БД).
    # Также исключаем тех, кому урок уже доставлен через marathon_queue сегодня —
    # fallback на случай если notification_sent_at не был выставлен (краш, race).
    learning_pool_inst = await get_learning_pool()
    async with learning_pool_inst.acquire() as lconn:
        notified_rows = await lconn.fetch('''
            SELECT DISTINCT chat_id FROM marathon_content
            WHERE notification_sent_at >= $1::date
        ''', today_msk)
        queue_delivered_rows = await lconn.fetch('''
            SELECT DISTINCT user_id AS chat_id FROM learning.marathon_queue
            WHERE content_type = 'lesson_practice'
              AND status = 'sent'
              AND sent_at >= $1::date
        ''', today_msk)
    notified_chat_ids = list(
        {r['chat_id'] for r in notified_rows} | {r['chat_id'] for r in queue_delivered_rows}
    )

    # Шаг 2: candidates из user_state, исключая уже notified.
    pool = await get_pool()
    async with pool.acquire() as conn:
        missed = await conn.fetch('''
            SELECT s.chat_id, s.completed_topics
            FROM development.user_state s
            WHERE s.marathon_status = 'active'
              AND s.onboarding_completed = TRUE
              AND s.schedule_time IS NOT NULL
              AND s.schedule_time <= $1
              AND s.bot_blocked IS NOT TRUE
              AND NOT (s.chat_id = ANY($2::bigint[]))
        ''', time_str, notified_chat_ids)

    if not missed:
        return

    # Шаг 3: исключить пользователей нового движка (learning.marathon_progress).
    # get_all_scheduled_interns делает это через app-side join; здесь тот же фильтр
    # чтобы избежать race condition: catch-up может запуститься до того, как
    # marathon_queue обновит status='sent', и повторно доставит Day 1 старым движком.
    missed_ids = [r['chat_id'] for r in missed]
    async with learning_pool_inst.acquire() as lconn:
        progress_rows = await lconn.fetch(
            'SELECT user_id FROM learning.marathon_progress WHERE user_id = ANY($1::bigint[])',
            missed_ids
        )
    newcomer_ids = {r['user_id'] for r in progress_rows}
    missed = [r for r in missed if r['chat_id'] not in newcomer_ids]

    if not missed:
        return

    # Фильтруем пользователей, которые завершили все темы — их marathon_content
    # не создаётся (нет следующей темы), но catch-up не должен слать им поздравления.
    # Также фильтруем тех, кто уже достиг дневного лимита тем — catch-up бесполезен.
    from core.topics import get_total_topics
    from db.queries.users import get_topics_today as _get_topics_today
    total = get_total_topics()
    filtered = []
    for row in missed:
        try:
            completed = json.loads(row['completed_topics'] or '[]')
        except (ValueError, TypeError):
            completed = []
        if len(completed) >= total:
            logger.info(f"[Scheduler] Catch-up skip {row['chat_id']}: marathon already completed ({len(completed)}/{total} topics), fixing status")
            # Auto-fix: обновляем статус, который не был обновлён ранее
            try:
                intern = await get_intern(row['chat_id'])
                feed_status = intern.get('feed_status', 'not_started') if intern else 'not_started'
                new_mode = derive_mode(MarathonStatus.COMPLETED, feed_status)
                await update_intern(row['chat_id'], marathon_status=MarathonStatus.COMPLETED, mode=new_mode)
                logger.info(f"[Scheduler] Auto-fixed {row['chat_id']}: marathon_status → completed")
            except Exception as e:
                logger.error(f"[Scheduler] Auto-fix failed for {row['chat_id']}: {e}")
            continue
        # Проверяем дневной лимит тем — если исчерпан, catch-up бесполезен
        intern = await get_intern(row['chat_id'])
        if intern:
            topics_today = _get_topics_today(intern)
            if topics_today >= MAX_TOPICS_PER_DAY:
                logger.info(f"[Scheduler] Catch-up skip {row['chat_id']}: daily topic limit reached ({topics_today}/{MAX_TOPICS_PER_DAY})")
                continue
        filtered.append(row['chat_id'])

    if not filtered:
        return

    logger.warning(f"[Scheduler] Catch-up: {len(filtered)} missed marathon deliveries, re-triggering")

    bot = Bot(token=_bot_token)
    sem = asyncio.Semaphore(10)

    async def _deliver_one(chat_id: int):
        async with sem:
            try:
                if await send_scheduled_topic(chat_id, bot):
                    intern_chk = await get_intern(chat_id)
                    exhausted = intern_chk.get('retry_exhausted_date') if intern_chk else None
                    if exhausted and exhausted >= moscow_now().date():
                        logger.info(f"[Scheduler] {chat_id}: retry exhausted today, skip catch-up chain")
                    else:
                        _schedule_retry(chat_id, 'marathon', attempt=0)
                else:
                    logger.info(f"[Scheduler] Catch-up delivered to {chat_id}")
            except Exception as e:
                if _is_user_unavailable(e):
                    await _handle_unavailable_user(chat_id, "catch-up")
                else:
                    logger.error(f"[Scheduler] Catch-up failed for {chat_id}: {e}")

    try:
        await asyncio.gather(*[_deliver_one(cid) for cid in filtered])
    finally:
        await bot.session.close()


# ═══════════════════════════════════════════════════════════
# SCHEDULE INTEGRITY CHECK
# ═══════════════════════════════════════════════════════════

async def _check_schedule_integrity(now) -> Optional[str]:
    """Daily integrity check: detect users with broken schedule data.

    Checks:
    1. Non-zero-padded schedule_time/feed_schedule_time (e.g. '7:30' instead of '07:30')
    2. Active marathon/feed users whose delivery time already passed today but got nothing
    3. Contradictory states (e.g. marathon_status='not_started' but completed_topics > 0)
    """
    from db.connection import get_pool

    pool = await get_pool()
    issues = []

    async with pool.acquire() as conn:
        # 0. Сброс вчерашних exhaustion-маркеров (F2)
        cleared = await conn.fetchval(
            "WITH d AS (UPDATE development.user_state SET retry_exhausted_date = NULL "
            "WHERE retry_exhausted_date < CURRENT_DATE RETURNING 1) SELECT COUNT(*) FROM d"
        )
        if cleared:
            logger.info(f"[Integrity] Cleared retry_exhausted_date for {cleared} users")

        # 1. Non-zero-padded times
        bad_times = await conn.fetch('''
            SELECT s.chat_id, u.tg_username, s.schedule_time, s.feed_schedule_time
            FROM development.user_state s
            JOIN public.users u ON u.telegram_id = s.chat_id
            WHERE s.onboarding_completed = TRUE
              AND (s.schedule_time ~ '^[0-9]:' OR s.feed_schedule_time ~ '^[0-9]:')
        ''')
        if bad_times:
            for r in bad_times:
                issues.append(f"⚠️ {html.escape(str(r['tg_username'] or r['chat_id']))}: "
                              f"schedule={r['schedule_time']}, feed={r['feed_schedule_time']} (no leading zero)")
            # Auto-fix
            await conn.execute("UPDATE development.user_state SET schedule_time = LPAD(schedule_time, 5, '0') WHERE schedule_time ~ '^[0-9]:'")
            await conn.execute("UPDATE development.user_state SET feed_schedule_time = LPAD(feed_schedule_time, 5, '0') WHERE feed_schedule_time ~ '^[0-9]:'")

        # 2. Contradictory states: has progress but status = 'not_started'
        contradictions = await conn.fetch('''
            SELECT s.chat_id, u.tg_username, s.marathon_status, s.feed_status,
                   s.current_topic_index, s.completed_topics, s.marathon_start_date
            FROM development.user_state s
            JOIN public.users u ON u.telegram_id = s.chat_id
            WHERE s.onboarding_completed = TRUE
              AND (
                (s.marathon_status = 'not_started' AND s.marathon_start_date IS NOT NULL)
                OR (s.marathon_status = 'not_started' AND s.current_topic_index > 0)
              )
        ''')
        # Auto-fix: users with start_date <= today and marathon_status='not_started' → set to 'active'
        fixable = [r for r in contradictions
                   if r['marathon_start_date'] is not None
                   and r['marathon_start_date'] <= now.date()]
        if fixable:
            fix_ids = [r['chat_id'] for r in fixable]
            await conn.execute(
                "UPDATE development.user_state SET marathon_status = 'active' WHERE chat_id = ANY($1::bigint[])",
                fix_ids,
            )
            for r in fixable:
                issues.append(f"🟢 {html.escape(str(r['tg_username'] or r['chat_id']))}: "
                              f"auto-fixed marathon_status → active "
                              f"(had {r['current_topic_index']} topics)")

        # Report remaining (no progress, just start_date set)
        unfixable = [r for r in contradictions if r not in fixable]
        for r in unfixable:
            issues.append(f"🔴 {html.escape(str(r['tg_username'] or r['chat_id']))}: "
                          f"marathon_status={r['marathon_status']} but "
                          f"start_date={r['marathon_start_date']}, topic_index={r['current_topic_index']}")

    if not issues:
        return None

    header = f"🔍 <b>Schedule Integrity ({now.strftime('%d.%m %H:%M')})</b>\n\n"
    return header + "\n".join(issues[:20])  # cap at 20 issues


# ═══════════════════════════════════════════════════════════
# NEON KEEP-ALIVE
# ═══════════════════════════════════════════════════════════

async def _neon_keep_alive():
    """Пинг каждые 4 минуты — предотвращение Neon idle suspend (cold start).

    ВАЖНО (WP-330 латентность, peer-session 2026-06-03-18): get_pool() =
    DATABASE_URL = bot_data (Railway-local Postgres) — он НЕ suspend'ится,
    пинговать его для борьбы с cold-start бесполезно. Реальные cold-start'ы —
    на Neon serverless пулах (learning/journal/persona/consent/indicators/...),
    которые делят ОДИН Neon compute endpoint (ep-dark-hall). Пинг любого Neon-пула
    держит общий compute «тёплым» → нет multi-секундного wake на nav-командах
    (/settings и /start трогают persona+consent = Neon).
    """
    # 1. Neon compute (shared endpoint) — это то, что реально засыпает.
    #    Один пинг learning держит весь Neon-compute тёплым для всех Neon-БД.
    try:
        from db.connection import get_learning_pool
        npool = await get_learning_pool()
        async with npool.acquire() as conn:
            await conn.fetchval('SELECT 1')
    except Exception as e:
        logger.warning(f"[Scheduler] Neon keep-alive (learning) failed: {e}")
    # 2. bot_data (Railway-local) — поддержать пул живым (не suspend, но дёшево).
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval('SELECT 1')
    except Exception as e:
        logger.warning(f"[Scheduler] keep-alive (bot_data) failed: {e}")


async def _rollback_expired_burn_reservations():
    """WP-327: откат резервов бонусов старше 30 минут (status='reserved' без confirm/cancel).

    Защита от «зависших» резервов: пилот нажал «Применить», но не пошёл по ссылке оплаты —
    через 30 мин бонусы возвращаются. Запускается каждые 5 мин (см. start_scheduler)."""
    try:
        from db.queries.redeem import rollback_expired_reservations
        count = await rollback_expired_reservations()
        if count > 0:
            logger.info(f"[Scheduler] WP-327: rolled back {count} expired burn reservations")
    except Exception as e:
        logger.warning(f"[Scheduler] rollback_expired_burn_reservations failed: {e}")


async def _better_stack_heartbeat():
    """WP-244 — пинг Better Stack heartbeat каждую минуту.

    Если бот лежит >grace (180s) — Better Stack создаёт incident,
    наш CF Worker observability-webhook постит «🔴 Бот недоступен» в @aisystant_status.
    URL берётся из env BETTER_STACK_HEARTBEAT_URL (опц.; пустой = no-op).
    """
    import os
    url = os.getenv("BETTER_STACK_HEARTBEAT_URL", "").strip()
    if not url:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.head(url) as r:
                if r.status >= 400:
                    logger.warning(f"[Heartbeat] BS returned {r.status}")
    except Exception as e:
        # Не валим scheduler из-за heartbeat. Если bot жив, но BS не отвечает — пропуск.
        logger.warning(f"[Heartbeat] BS ping failed: {e}")


async def _send_slot_daily_prompt():
    """Ф13c (WP-310): 22:00 МСК — напоминание пилотам без slot_logged за сегодня.

    Целевые пользователи: все, кто хоть раз логировал слот (пилоты), но
    НЕ залогировали сегодня (МСК). created_at + 3ч = перевод UTC → МСК.
    """
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT user_id
                FROM development.user_events
                WHERE event_type = 'slot_logged'
                  AND user_id NOT IN (
                    SELECT DISTINCT user_id
                    FROM development.user_events
                    WHERE event_type = 'slot_logged'
                      AND DATE_TRUNC('day', created_at + INTERVAL '3 hours')
                          = DATE_TRUNC('day', NOW() + INTERVAL '3 hours')
                  )
            """)

        if not rows:
            logger.info("[SlotPrompt] Все пилоты уже залогировали сегодня — prompt не нужен")
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="30 мин", callback_data="slot_daily:0.5"),
            InlineKeyboardButton(text="1 ч", callback_data="slot_daily:1.0"),
            InlineKeyboardButton(text="2 ч", callback_data="slot_daily:2.0"),
        ], [
            InlineKeyboardButton(text="Не учился сегодня", callback_data="slot_daily:skip"),
        ]])

        bot = Bot(token=_bot_token)
        try:
            sent = skipped = 0
            for row in rows:
                try:
                    await bot.send_message(
                        row["user_id"],
                        "Учился сегодня? Запиши время — оно идёт в твою ступень.",
                        reply_markup=kb,
                    )
                    sent += 1
                except Exception as e:
                    skipped += 1
                    logger.debug(f"[SlotPrompt] user_id={row['user_id']}: {e}")
            logger.info(f"[SlotPrompt] 22:00 МСК: sent={sent} skipped={skipped}")
        finally:
            await bot.session.close()
    except Exception as e:
        logger.exception(f"[SlotPrompt] Error in _send_slot_daily_prompt: {e}")


# ═══════════════════════════════════════════════════════════
# DIGITAL TWIN: PROACTIVE TOKEN REFRESH (WP-82)
# ═══════════════════════════════════════════════════════════

async def _gateway_proactive_refresh():
    """Обновить Ory tokens для Gateway MCP (WP-209 Ф0).

    Запускается каждые 10 мин. Ory access token TTL ~ 1 час.
    """
    try:
        from clients.gateway_mcp import gateway_mcp
        await gateway_mcp.refresh_expiring_tokens(margin_seconds=600)
    except Exception as e:
        logger.warning(f"[Scheduler] Gateway Ory refresh failed: {e}")


# ═══════════════════════════════════════════════════════════
# WP-253 GAP C: ONE-TIME GITHUB RELINK NOTIFICATION
# ═══════════════════════════════════════════════════════════

async def _notify_github_relink():
    """One-time notification to users who had GitHub connected pre-Gap-C cutover.

    Finds users in persona.user_integrations (service='github') that are missing
    from secrets.github_connections — they need to re-link via /github.
    Dedup: skips users already logged in development.user_events (source='github_relink_notif').
    """
    try:
        from db.queries.github import get_users_needing_github_relink
        from db.connection import get_pool
        import uuid as _uuid

        users = await get_users_needing_github_relink()
        if not users:
            logger.info("[GithubRelink] No users needing relink notification")
            return

        bot_pool = await get_pool()
        sent = 0
        skipped = 0
        for u in users:
            chat_id = u["chat_id"]
            try:
                # Atomic log-before-send (§10.10): INSERT ... RETURNING
                # Если INSERT вернул строку — мы первые → слать. Пусто → уже отправлено.
                async with bot_pool.acquire() as conn:
                    inserted = await conn.fetchrow(
                        """INSERT INTO development.user_events
                               (user_id, user_uuid, event_type, source, payload,
                                confidence, created_at, external_id)
                           VALUES (0, $1, 'notification_sent', 'github_relink_notif',
                                   '{}', 1.0, NOW(), $2)
                           ON CONFLICT (source, external_id)
                               WHERE external_id IS NOT NULL
                           DO NOTHING
                           RETURNING id""",
                        _uuid.UUID(str(u["account_id"])),
                        str(chat_id),
                    )
                if not inserted:
                    skipped += 1
                    continue

                text = (
                    "🔗 <b>Требуется повторное подключение GitHub</b>\n\n"
                    "После обновления платформы ваше подключение GitHub нужно обновить. "
                    "Нажмите /github чтобы переподключить — это займёт 30 секунд."
                )
                await bot.send_message(chat_id, text, parse_mode="HTML")
                sent += 1
            except Exception as e:
                logger.warning("[GithubRelink] Failed to notify chat_id=%s: %s", chat_id, e)

        logger.info("[GithubRelink] Notifications sent=%d skipped(already_notified)=%d", sent, skipped)
    except Exception as e:
        logger.error("[GithubRelink] Notification job failed: %s", e)


# DIGITAL TWIN ENGAGEMENT SYNC (WP-85 Phase 4)
# ═══════════════════════════════════════════════════════════

async def _dt_sync_engagement():
    """Синхронизировать engagement данные → digital_twins JSONB.

    Запускается ежедневно в 04:30 MSK. Читает development.engagement view,
    пишет в digital_twins таблицу (та же Neon БД). DT MCP читает при запросе.
    """
    try:
        from db.queries.dt_sync import sync_engagement_to_dt
        stats = await sync_engagement_to_dt()
        logger.info(f"[Scheduler] DT engagement sync: {stats}")
    except Exception as e:
        logger.error(f"[Scheduler] DT engagement sync failed: {e}")


# ═══════════════════════════════════════════════════════════
# DIGITAL TWIN SYNC RETRY
# ═══════════════════════════════════════════════════════════

async def _sync_dt_connected_users():
    """Проверяет подключённых к ЦД пользователей и досинхронизирует профиль."""
    from clients.gateway_mcp import gateway_mcp
    from db.queries.users import get_intern

    connected_ids = gateway_mcp.get_connected_user_ids()
    if not connected_ids:
        return

    for user_id in connected_ids:
        try:
            intern = await get_intern(user_id)
            if intern:
                await gateway_mcp.sync_profile(user_id, intern)
        except Exception as e:
            logger.error(f"[DT Sync] Retry failed for user {user_id}: {e}")


# ═══════════════════════════════════════════════════════════
# MILESTONE NOTIFICATIONS (DP.ARCH.002 § 12.5, C3)
# ═══════════════════════════════════════════════════════════

async def send_milestone_notifications():
    """Отправить milestone-уведомления (C3): 7/14/30/60/90 дней."""
    import json
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from db.queries.conversion import (
        get_milestone_eligible_users, log_conversion_event, MILESTONE_DAYS,
    )
    from config.settings import PLATFORM_URLS

    bot = Bot(token=_bot_token)
    total_sent = 0

    try:
        for day in MILESTONE_DAYS:
            milestone = f"day_{day}"
            users = await get_milestone_eligible_users(day)

            for user in users:
                chat_id = user['chat_id']
                lang = user.get('language', 'ru') or 'ru'

                try:
                    completed = json.loads(user.get('completed_topics', '[]') or '[]')
                except (json.JSONDecodeError, TypeError):
                    completed = []
                topics_count = len(completed)
                active_days = user.get('active_days_total', 0) or 0
                streak = user.get('longest_streak', 0) or 0
                bloom = user.get('complexity_level', 1) or 1

                # Базовое сообщение
                encouragement = ''
                if day == 7:
                    if active_days > 0 or topics_count > 0:
                        encouragement = t('milestones.day_7_active', lang)
                    else:
                        encouragement = t('milestones.day_7_inactive', lang)

                text = t(f'milestones.day_{day}', lang,
                         topics=topics_count,
                         active_days=active_days,
                         streak=streak,
                         bloom=bloom,
                         marathon_status='',
                         encouragement=encouragement)

                # Специальные вставки для day_14
                if day == 14:
                    marathon_done = user.get('marathon_status') == 'completed'
                    if marathon_done:
                        ms = t('milestones.day_14_marathon_done', lang)
                    else:
                        ms = t('milestones.day_14_marathon_progress', lang,
                               completed=topics_count)
                    text = text.replace('{marathon_status}', ms)

                # Кнопки: day_30 и ниже → предложить ЛР, day_60 → mydata
                keyboard = None
                if day in (30, 90):
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text=t('milestones.btn_program', lang),
                            url=PLATFORM_URLS['lr'],
                        )]
                    ])
                elif day == 60:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text=t('milestones.btn_mydata', lang),
                            callback_data="cmd_mydata",
                        )]
                    ])
                elif day == 14:
                    marathon_done = user.get('marathon_status') == 'completed'
                    if marathon_done:
                        keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text=t('milestones.btn_program', lang),
                                url=PLATFORM_URLS['lr'],
                            )]
                        ])

                try:
                    # WP-152: двойная запись в notification_log
                    from db.queries.notifications import try_insert_notification
                    milestone_key = f"milestone:{chat_id}:{milestone}"
                    if not await try_insert_notification(chat_id, 'milestone', milestone_key):
                        logger.info(f"[Scheduler] Milestone {milestone} already sent to {chat_id}, skip")
                        continue

                    # Логируем ПЕРЕД отправкой — предотвращает дубль при retry (legacy)
                    await log_conversion_event(chat_id, 'C3', milestone)
                    await bot.send_message(
                        chat_id, text,
                        reply_markup=keyboard,
                        parse_mode="Markdown",
                    )
                    total_sent += 1
                    logger.info(f"[Scheduler] Milestone {milestone} sent to {chat_id}")
                except Exception as e:
                    if _is_user_unavailable(e):
                        await _handle_unavailable_user(chat_id, f"milestone {milestone}")
                    else:
                        logger.error(f"[Scheduler] Milestone {milestone} error for {chat_id}: {e}")
    finally:
        await bot.session.close()

    if total_sent > 0:
        logger.info(f"[Scheduler] Milestone notifications: {total_sent} sent")


# ═══════════════════════════════════════════════════════════
# ENGAGEMENT NUDGES (WP-85 Phase 5C)
# ═══════════════════════════════════════════════════════════

async def _try_send_upgrade_nudge(
    bot,
    chat_id: int,
    ory_uuid: str,
    state: dict,
    lang: str,
) -> bool:
    """Evaluate F/G upgrade markers and send rich CTA if triggered.

    F: T2+14d active, guide not opened → тайный гит (T2→T3).
    G: T3+30d active, guide opened (or ≥21d proxy) → явный гит (T3→T4).
    Returns True if a nudge was sent. Writes msg_{f|g}_sent_at atomically on success.
    See WP-349 Ф6/Ф7, peer report sessions/conversations/2026-05-22-04-wp349-markers-fg.
    """
    from handlers.tier_upgrade import UPGRADE_NUDGE_SENDERS
    from db.queries.onboarding_journey import write_upgrade_sent_at

    days = state.get("activity_days_count") or 0

    # dict в insertion order — первый сработавший маркер отправляется (F приоритетнее G)
    markers: list[tuple[str, bool]] = [
        (
            "f",
            bool(
                state.get("has_subscription") and
                days >= 14 and
                not state.get("first_use_guide_render") and
                not state.get("first_use_connect_full") and
                state.get("msg_f_sent_at") is None
            ),
        ),
        (
            "g",
            bool(
                state.get("has_subscription") and
                (state.get("first_use_guide_render") or days >= 21) and
                # FIXME-T2-leak: days>=21 не отличает T3b от стойкого T2.
                # При миграции 238 заменить на has_github_connected.
                days >= 30 and
                not state.get("first_use_connect_full") and
                state.get("msg_g_sent_at") is None
            ),
        ),
    ]

    for key, fires in markers:
        if not fires:
            continue
        sender = UPGRADE_NUDGE_SENDERS.get(key)
        if sender is None:
            logger.warning("[Nudge] No sender for upgrade marker %s", key)
            continue
        sent = await sender(bot, chat_id, days, lang)
        if sent:
            await write_upgrade_sent_at(ory_uuid, key)
        return sent  # first fired marker: return True/False, don't try next

    return False


async def send_engagement_nudges():
    """Проанализировать engagement-данные T3+ и отправить nudge-уведомления."""
    import json
    from core.engagement_analyzer import analyze
    from db.queries.nudges import get_nudge_candidates
    from db.queries.notifications import (
        was_nudge_sent_recently, try_insert_notification,
    )
    from i18n import t

    candidates = await get_nudge_candidates()
    if not candidates:
        return

    # WP-349: batch-check onboarding_controller cooldown to prevent dual nudges.
    # onboarding_controller marks last_nudge_at in learning.onboarding_state;
    # if it nudged this pilot today, the bot should not send an additional nudge.
    onboarding_nudged_uuids: set[str] = set()
    upgrade_state_map: dict[str, dict] = {}  # ory_uuid → onboarding_state fields for F/G
    ory_uuids = [u['ory_uuid'] for u in candidates if u.get('ory_uuid')]
    if ory_uuids:
        try:
            from db.connection import get_learning_pool
            learning_pool = await get_learning_pool()
            async with learning_pool.acquire() as lconn:
                cooldown_rows = await lconn.fetch(
                    "SELECT account_id::text FROM learning.onboarding_state "
                    "WHERE last_nudge_at > NOW() - INTERVAL '24 hours' "
                    "AND account_id = ANY($1::uuid[])",
                    ory_uuids,
                )
                onboarding_nudged_uuids = {r['account_id'] for r in cooldown_rows}
                # WP-349 Ф6/Ф7: batch-fetch state for F/G marker evaluation
                upgrade_rows = await lconn.fetch(
                    "SELECT account_id::text, activity_days_count, has_subscription, "
                    "first_use_guide_render, first_use_connect_full, "
                    "msg_f_sent_at, msg_g_sent_at "
                    "FROM learning.onboarding_state "
                    "WHERE account_id = ANY($1::uuid[])",
                    ory_uuids,
                )
                upgrade_state_map = {r['account_id']: dict(r) for r in upgrade_rows}
            if onboarding_nudged_uuids:
                logger.info(f"[Nudge] Onboarding cooldown: skipping {len(onboarding_nudged_uuids)} pilots nudged today by controller")
        except Exception as e:
            logger.warning(f"[Nudge] Onboarding cooldown check failed (fail-open): {e}")

    bot = Bot(token=_bot_token)
    total_sent = 0

    try:
        for user in candidates:
            chat_id = user['chat_id']
            lang = user.get('language', 'ru') or 'ru'

            # Skip if onboarding_controller already nudged today (WP-349 dual-cooldown fix)
            ory_uuid = user.get('ory_uuid')
            if ory_uuid and ory_uuid in onboarding_nudged_uuids:
                logger.debug(f"[Nudge] Skipping {chat_id}: onboarding nudge sent today")
                continue

            # WP-349 Ф6/Ф7: upgrade nudge (F/G rich CTA) takes priority over engagement nudge
            if ory_uuid:
                ustate = upgrade_state_map.get(ory_uuid)
                if ustate:
                    try:
                        upgrade_sent = await _try_send_upgrade_nudge(bot, chat_id, ory_uuid, ustate, lang)
                        if upgrade_sent:
                            total_sent += 1
                            continue  # one nudge per user per day
                    except Exception as e:
                        if _is_user_unavailable(e):
                            await _handle_unavailable_user(chat_id, "upgrade_nudge")
                            continue
                        logger.error(f"[Nudge] Upgrade nudge error for {chat_id}: {e}")

            # Parse engagement JSONB
            engagement = user.get('engagement')
            if engagement and isinstance(engagement, str):
                try:
                    engagement = json.loads(engagement)
                except (json.JSONDecodeError, TypeError):
                    engagement = {}
            engagement = engagement or {}

            # Parse derived JSONB (WP-151 Ф4)
            derived = user.get('derived')
            if derived and isinstance(derived, str):
                try:
                    derived = json.loads(derived)
                except (json.JSONDecodeError, TypeError):
                    derived = {}
            derived = derived or {}

            # User meta for analyzer
            user_meta = {
                'last_active_date': user.get('last_active_date'),
                'active_days_total': user.get('active_days_total'),
                'active_days_streak': user.get('active_days_streak'),
                'longest_streak': user.get('longest_streak'),
                'marathon_status': user.get('marathon_status'),
                'last_slot_date': user.get('last_slot_at'),  # WP-117 Этап 1: slot_missing_3d
            }

            # Run rules (basic + derived-aware)
            nudges = analyze(engagement, user_meta, derived)
            if not nudges:
                continue

            # Pick first applicable (respecting cooldown)
            for nudge in nudges:
                rule_id = nudge['rule_id']
                nudge_key = nudge['nudge_key']
                cooldown = nudge['cooldown_days']

                if await was_nudge_sent_recently(chat_id, nudge_key, cooldown):
                    continue

                # Build message
                i18n_key = f'nudges.{nudge_key}'
                text = t(i18n_key, lang,
                         name=user.get('name', ''),
                         active_days=user_meta.get('active_days_total', 0),
                         streak=user_meta.get('longest_streak', 0))

                # Skip if i18n key missing (returns raw key)
                if text == i18n_key or nudge_key in text:
                    logger.warning(f"[Nudge] Missing i18n key: {i18n_key}")
                    continue

                try:
                    today_str = moscow_now().strftime('%Y-%m-%d')
                    notif_key = f"nudge:{chat_id}:{today_str}:{nudge_key}"
                    inserted = await try_insert_notification(chat_id, 'nudge', notif_key)
                    if not inserted:
                        # Дубль — уже отправляли сегодня
                        continue

                    await bot.send_message(chat_id, text, parse_mode="Markdown")
                    total_sent += 1
                    logger.info(f"[Nudge] Sent {nudge_key} to {chat_id}")
                    break  # One nudge per user per day
                except Exception as e:
                    if _is_user_unavailable(e):
                        await _handle_unavailable_user(chat_id, "nudge")
                    else:
                        logger.error(f"[Nudge] Error for {chat_id}: {e}")
                    break  # Don't retry other nudges for this user
    finally:
        await bot.session.close()

    if total_sent > 0:
        logger.info(f"[Nudge] Total sent: {total_sent}")


# ═══════════════════════════════════════════════════════════
# EVENT NOTIFICATIONS (DP.ARCH.002 § 12.7, C7)
# ═══════════════════════════════════════════════════════════

async def send_event_notifications():
    """Уведомить всех активных пользователей о приближающихся событиях (C7)."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from config.conversion import get_upcoming_events
    from db.queries.conversion import log_conversion_event, was_milestone_sent
    from db.connection import get_pool

    today = moscow_today()
    events = get_upcoming_events(today)
    if not events:
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT s.chat_id, u.language
               FROM development.user_state s
               JOIN public.users u ON u.telegram_id = s.chat_id
               WHERE s.onboarding_completed = TRUE'''
        )

    bot = Bot(token=_bot_token)
    total_sent = 0

    try:
        for event in events:
            event_name = event.get("name_ru", "")
            event_url = event.get("url", "")
            days_until = event.get("days_until", 0)
            event_date = event["date"].strftime("%d.%m")
            milestone_key = f"event:{event_name[:40]}"

            for row in rows:
                chat_id = row['chat_id']
                lang = row.get('language', 'ru') or 'ru'

                # Dedup: не отправляли ли уже C7 для этого события
                if await was_milestone_sent(chat_id, milestone_key):
                    continue

                name = event.get(f"name_{lang}", event_name)
                if lang == 'ru':
                    text = (
                        f"📅 *Событие через {days_until} дн. ({event_date})*\n\n"
                        f"*{name}*\n\n"
                        f"Зарегистрироваться можно по ссылке ниже."
                    )
                else:
                    text = (
                        f"📅 *Event in {days_until} days ({event_date})*\n\n"
                        f"*{name}*\n\n"
                        f"Register using the link below."
                    )

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="📅 " + ("Зарегистрироваться" if lang == 'ru' else "Register"),
                        url=event_url,
                    )]
                ])

                try:
                    # WP-152: двойная запись в notification_log
                    from db.queries.notifications import try_insert_notification
                    event_notif_key = f"event:{chat_id}:{milestone_key}"
                    if not await try_insert_notification(chat_id, 'event', event_notif_key):
                        continue

                    # Логируем ПЕРЕД отправкой — предотвращает дубль при retry (legacy)
                    await log_conversion_event(chat_id, 'C7', milestone_key)
                    await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")
                    total_sent += 1
                except Exception as e:
                    if _is_user_unavailable(e):
                        await _handle_unavailable_user(chat_id, "event notification")
                    else:
                        logger.error(f"[Scheduler] Event notification error for {chat_id}: {e}")
    finally:
        await bot.session.close()

    if total_sent > 0:
        logger.info(f"[Scheduler] Event notifications: {total_sent} sent for {len(events)} events")


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


# ═══════════════════════════════════════════════════════════
# DISCOURSE: запланированные публикации + мониторинг комментариев (WP-53)
# ═══════════════════════════════════════════════════════════

async def _discourse_scheduled_publish():
    """Публиковать запланированные посты (каждые ~30 мин, в :07 и :37)."""
    from clients.discourse import discourse
    if not discourse:
        return

    from db.queries.discourse import (
        get_pending_publications, mark_publication_done, mark_publication_failed,
        save_published_post,
    )

    pubs = await get_pending_publications()
    if not pubs:
        return

    bot = Bot(token=_bot_token)
    try:
        for pub in pubs:
            try:
                raw = pub["raw"]
                source_file = pub.get("source_file")
                gh_client = None

                # Единый GitHub-клиент для cover + frontmatter (S48 refactor)
                if source_file:
                    try:
                        from pathlib import Path as _Path
                        from clients.github_content import create_content_client, update_frontmatter_field
                        from clients.github_oauth import github_oauth
                        _token = await github_oauth.get_access_token(pub["chat_id"])
                        _repo = await github_oauth.get_knowledge_repo(pub["chat_id"])
                        if _token and _repo:
                            gh_client = create_content_client(_token, _repo)
                            # Cover (S48)
                            try:
                                cover_path = str(_Path(source_file).parent / "cover.png")
                                cover_bytes = await gh_client.read_binary_file(cover_path)
                                if cover_bytes:
                                    cover_md = await discourse.upload_image(
                                        "cover.png", cover_bytes, pub["discourse_username"]
                                    )
                                    if cover_md:
                                        raw = f"{cover_md}\n\n{raw}"
                                        logger.info(f"[Publisher] Cover image prepended: {pub['title']!r}")
                            except Exception as cover_err:
                                logger.warning(f"[Publisher] Cover skip: {cover_err}")
                    except Exception as gh_err:
                        logger.warning(f"[Publisher] GitHub client init failed: {gh_err}")

                result = await discourse.create_topic(
                    category_id=pub["category_id"],
                    title=pub["title"],
                    raw=raw,
                    username=pub["discourse_username"],
                )
                topic_id = result.get("topic_id")
                post_id = result.get("id")

                await mark_publication_done(pub["id"], topic_id)
                await save_published_post(
                    chat_id=pub["chat_id"],
                    discourse_topic_id=topic_id,
                    discourse_post_id=post_id,
                    title=pub["title"],
                    category_id=pub["category_id"],
                    source_file=source_file,
                )

                # Обновить frontmatter → published (тот же gh_client)
                if source_file and gh_client:
                    try:
                        file_result = await gh_client.read_file(source_file)
                        if file_result:
                            content, sha = file_result
                            new_content = update_frontmatter_field(content, "status", "published")
                            await gh_client.update_file(
                                source_file, new_content, sha,
                                f"Published to club: {pub['title']}"
                            )
                    except Exception as fm_err:
                        logger.warning(f"[Publisher] Frontmatter update failed for {source_file}: {fm_err}")

                # Закрыть gh_client после всех операций
                if gh_client:
                    try:
                        await gh_client.close()
                    except Exception:
                        pass

                # Уведомить пользователя
                slug = result.get("topic_slug", "")
                url = f"https://systemsworld.club/t/{slug}/{topic_id}"

                from db.queries.discourse import get_scheduled_count
                queue_count = await get_scheduled_count(pub["chat_id"])

                await bot.send_message(
                    pub["chat_id"],
                    f"Опубликовано в клуб: «{pub['title']}»\n"
                    f"{url}\n"
                    f"В очереди: {queue_count}",
                )
                logger.info(f"[Publisher] Scheduled post published: topic_id={topic_id}, queue={queue_count}")
            except Exception as e:
                logger.error(f"[Discourse] Scheduled publish error for pub_id={pub['id']}: {e}")
                await mark_publication_failed(pub["id"])
    finally:
        await bot.session.close()


async def _smart_publisher_scan(*, notify: bool = True):
    """R21 Публикатор: ежедневный scan индекса знаний + auto-schedule (05:07 МСК).

    Цикл:
    1. Получить все discourse accounts
    2. Для каждого: scan GitHub index → найти ready+club посты
    3. Reconciliation: сверить с published_posts и scheduled_publications
    4. Auto-schedule новые посты на ближайшие свободные слоты
    5. Queue Watch: если pending < min_queue → уведомить (только при notify=True)

    Args:
        notify: Отправлять ли queue-watch уведомления. Cron (05:07) = True, startup scan = False.
    """

    from clients.github_content import create_content_client, parse_frontmatter
    from db.queries.github import get_users_with_knowledge_repo
    from db.queries.discourse import (
        get_all_discourse_accounts,
        get_all_published_source_files,
        get_all_published_titles_lower,
        get_all_scheduled_source_files,
        get_all_scheduled_titles_lower,
        get_scheduled_count,
        get_scheduled_dates,
        schedule_publication,
    )
    from config.settings import PUBLISHER_DAYS, PUBLISHER_TIME, PUBLISHER_INTERVAL, PUBLISHER_MIN_QUEUE

    knowledge_users = await get_users_with_knowledge_repo()
    if not knowledge_users:
        logger.info("[Publisher] Smart scan skipped: no users with knowledge_repo configured")
        return

    accounts = await get_all_discourse_accounts()
    if not accounts:
        logger.warning("[Publisher] Smart scan skipped: no discourse_accounts in DB")
        return

    # chat_id → discourse account для быстрого lookup
    account_map = {a["chat_id"]: a for a in accounts}

    bot = Bot(token=_bot_token)
    try:
        for ku in knowledge_users:
            chat_id = ku["chat_id"]
            token = ku["access_token"]
            knowledge_repo = ku["knowledge_repo"]

            # Нужен и Discourse аккаунт с blog_category_id
            account = account_map.get(chat_id)
            if not account:
                continue
            category_id = account.get("blog_category_id")
            if not category_id:
                continue

            # Per-user scan индекса знаний
            client = create_content_client(token, knowledge_repo)
            try:
                today = datetime.now().date()
                cutoff = today - timedelta(days=14)
                current_year = today.year
                all_posts = []

                def _is_recent(filename: str) -> bool:
                    try:
                        match = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', filename)
                        if not match:
                            return True
                        file_date = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
                        return file_date >= cutoff
                    except (ValueError, IndexError):
                        return True

                # Рекурсивный обход: год → месячные папки → файлы + подпапки (мультиканальные посты)
                year_path = f"docs/{current_year}"
                month_dirs = await client.list_dirs(year_path)
                # Fallback: если нет подпапок — сканировать плоско (обратная совместимость)
                scan_paths = [f"{year_path}/{d}" for d in month_dirs] if month_dirs else [year_path]

                all_files = []
                for scan_path in scan_paths:
                    files_in_dir = await client.list_files(scan_path)
                    all_files.extend(files_in_dir)
                    # Проверяем подпапки (мультиканальные посты)
                    sub_dirs = await client.list_dirs(scan_path)
                    for sd in sub_dirs:
                        sub_files = await client.list_files(f"{scan_path}/{sd}")
                        all_files.extend(sub_files)

                for f in all_files:
                    if f["name"] == "README.md" or not _is_recent(f["name"]):
                        continue
                    result = await client.read_file(f["path"])
                    if not result:
                        continue
                    content, sha = result
                    # Ранний выход: нет frontmatter → пропускаем
                    if not content.startswith("---"):
                        continue
                    fm = parse_frontmatter(content)
                    if fm.get("type") != "post":
                        continue
                    all_posts.append({
                        "path": f["path"],
                        "sha": sha,
                        "title": fm.get("title", f["name"]),
                        "status": fm.get("status", "draft"),
                        "target": fm.get("target", ""),
                        "tags": fm.get("tags", []),
                        "created": fm.get("created", ""),
                        "audience": fm.get("audience", ""),
                        "content": content,
                    })

                logger.info(f"[Publisher] Scanned {len(all_posts)} posts from {knowledge_repo} for chat_id={chat_id}")

                # Reconciliation: dedup by source_file AND title
                published_files = await get_all_published_source_files(chat_id)
                published_titles = await get_all_published_titles_lower(chat_id)
                scheduled_files = await get_all_scheduled_source_files(chat_id)
                scheduled_titles = await get_all_scheduled_titles_lower(chat_id)

                # Найти ready+club посты, которые ещё не опубликованы и не запланированы
                candidates = []
                for post in all_posts:
                    if post["status"] != "ready":
                        continue
                    if post["target"] != "club":
                        continue
                    title_lower = post["title"].lower()
                    if post["path"] in published_files:
                        continue
                    if title_lower in published_titles:
                        continue
                    if post["path"] in scheduled_files:
                        continue
                    if title_lower in scheduled_titles:
                        continue
                    candidates.append(post)

                if not candidates:
                    logger.info(f"[Publisher] No new candidates for chat_id={chat_id} (total posts={len(all_posts)}, published_files={len(published_files)}, published_titles={len(published_titles)}, scheduled_titles={len(scheduled_titles)})")
                    # Queue watch (только при notify=True, т.е. из cron, не из startup scan)
                    if notify:
                        queue_count = await get_scheduled_count(chat_id)
                        if queue_count < PUBLISHER_MIN_QUEUE:
                            drafts = [p["title"] for p in all_posts
                                      if p["status"] == "draft" and p["target"] == "club"]
                            draft_hint = ""
                            if drafts:
                                draft_hint = "\n\nДрафты для клуба:\n" + "\n".join(f"  • {t}" for t in drafts[:5])
                            await bot.send_message(
                                chat_id,
                                f"В очереди публикаций: {queue_count} (мин. {PUBLISHER_MIN_QUEUE}).\n"
                                f"Нужны новые посты со status: ready и target: club.{draft_hint}",
                            )
                    continue

                # Auto-schedule: распределить по ближайшим слотам
                from db.queries.users import moscow_now

                now_msk = moscow_now()

                # Парсинг каденции
                day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
                pub_days = [day_map[d.strip()] for d in PUBLISHER_DAYS.split(",") if d.strip() in day_map]
                if not pub_days:
                    pub_days = [0, 2, 4]  # Default: Пн/Ср/Пт

                hour, minute = 10, 0
                try:
                    parts = PUBLISHER_TIME.split(":")
                    hour, minute = int(parts[0]), int(parts[1])
                except (ValueError, IndexError):
                    pass

                # Разделить: итоги недели (тег "итоги-недели") vs обычные посты
                weekly_reviews = [p for p in candidates if "итоги-недели" in (p.get("tags") or [])]
                regular = [p for p in candidates if "итоги-недели" not in (p.get("tags") or [])]

                from clients.github_content import strip_frontmatter
                import json

                scheduled_posts = []

                # Weekly reviews → публикация сразу (ближайший цикл :07/:37)
                for wr in weekly_reviews:
                    slot_time = datetime.utcnow() + timedelta(minutes=1)  # Ближайший цикл подхватит
                    raw = strip_frontmatter(wr["content"])
                    tags_json = json.dumps(wr["tags"]) if isinstance(wr["tags"], list) else "[]"
                    await schedule_publication(
                        chat_id=chat_id,
                        title=wr["title"],
                        raw=raw,
                        category_id=category_id,
                        schedule_time=slot_time,
                        tags=tags_json,
                        source_file=wr["path"],
                    )
                    scheduled_posts.append((wr["title"], slot_time))
                    logger.info(f"[Publisher] Auto-scheduled weekly review: {wr['title']!r} → {slot_time}")

                # Regular → Вт-Вс (исключить Пн=0 из каденции)
                regular_pub_days = [d for d in pub_days if d != 0]
                if not regular_pub_days:
                    regular_pub_days = [1, 2, 3, 4, 5, 6]  # Вт-Вс

                scheduled_count = await get_scheduled_count(chat_id)
                occupied_dates = await get_scheduled_dates(chat_id)
                slots = []
                check_date = now_msk.date() + timedelta(days=1)  # Начинаем с завтра
                max_check = 60  # Не дальше 60 дней

                for _ in range(max_check):
                    if check_date.weekday() in regular_pub_days and check_date not in occupied_dates:
                        slot_time = datetime.combine(check_date, datetime.min.time().replace(hour=hour, minute=minute)) - timedelta(hours=3)  # MSK→UTC
                        slots.append(slot_time)
                        occupied_dates.add(check_date)  # Не дублировать в рамках одного scan
                        if len(slots) >= len(regular):
                            break
                        # Пропуск по интервалу (1 раз в N дней)
                        check_date += timedelta(days=PUBLISHER_INTERVAL)
                        continue
                    check_date += timedelta(days=1)

                for post, slot in zip(regular, slots):
                    raw = strip_frontmatter(post["content"])
                    tags_json = json.dumps(post["tags"]) if isinstance(post["tags"], list) else "[]"
                    await schedule_publication(
                        chat_id=chat_id,
                        title=post["title"],
                        raw=raw,
                        category_id=category_id,
                        schedule_time=slot,
                        tags=tags_json,
                        source_file=post["path"],
                    )
                    scheduled_posts.append((post["title"], slot))
                    logger.info(f"[Publisher] Auto-scheduled: {post['title']!r} → {slot}")

                # Уведомить
                if scheduled_posts:
                    lines = [f"  • «{title}» — {(slot + timedelta(hours=3)).strftime('%a %d %b, %H:%M')}" for title, slot in scheduled_posts]  # UTC→MSK
                    await bot.send_message(
                        chat_id,
                        f"Добавлено в график публикаций ({len(scheduled_posts)}):\n" + "\n".join(lines),
                    )

                # Queue Watch (только при notify=True)
                if notify:
                    new_queue = await get_scheduled_count(chat_id)
                    if new_queue < PUBLISHER_MIN_QUEUE:
                        drafts = [p["title"] for p in all_posts
                                  if p["status"] == "draft" and p["target"] == "club"]
                        draft_hint = ""
                        if drafts:
                            draft_hint = "\n\nДрафты для клуба:\n" + "\n".join(f"  • {t}" for t in drafts[:5])
                        await bot.send_message(
                            chat_id,
                            f"В очереди: {new_queue} (мин. {PUBLISHER_MIN_QUEUE}). Нужны новые посты!{draft_hint}",
                        )

            except Exception as e:
                logger.error(f"[Publisher] Scan error for chat_id={chat_id}, repo={knowledge_repo}: {e}", exc_info=True)
            finally:
                await client.close()

    except Exception as e:
        logger.error(f"[Publisher] Smart scan error: {e}", exc_info=True)
    finally:
        await bot.session.close()


async def _discourse_check_comments():
    """Проверить новые комментарии к опубликованным постам (каждый час)."""
    from clients.discourse import discourse
    if not discourse:
        return

    from db.queries.discourse import (
        get_posts_for_comment_check,
        update_post_comments_count,
        increment_comment_check_failures,
    )

    posts = await get_posts_for_comment_check()
    if not posts:
        return

    bot = Bot(token=_bot_token)
    try:
        for post in posts:
            topic_id = post.get("discourse_topic_id")
            try:
                topic = await discourse.get_topic(topic_id)
                if not topic:
                    await increment_comment_check_failures(topic_id)
                    logger.info(f"[Discourse] Topic {topic_id} not found, failures incremented")
                    continue

                new_count = topic.get("posts_count", 1)
                old_count = post.get("posts_count", 1)

                if new_count > old_count:
                    # Есть новые комментарии
                    await update_post_comments_count(topic_id, new_count)

                    diff = new_count - old_count
                    slug = topic.get("slug", "")
                    url = f"https://systemsworld.club/t/{slug}/{topic_id}"
                    title = post.get("title", "")

                    word = "комментарий" if diff == 1 else "комментариев" if diff > 4 else "комментария"
                    await bot.send_message(
                        post["chat_id"],
                        f"Новый {word} ({diff}) к посту *{title}*\n\n{url}",
                        parse_mode="Markdown",
                    )
                    logger.info(f"[Discourse] New comments for topic {topic_id}: {old_count} -> {new_count}")
                elif new_count == old_count:
                    # Обновить last_checked_at
                    await update_post_comments_count(topic_id, new_count)
            except Exception as e:
                await increment_comment_check_failures(topic_id)
                logger.warning(f"[Discourse] Comment check error for topic {topic_id}: {e}")
    finally:
        await bot.session.close()


async def _discourse_typing_collect():
    """WP-327 Phase 3б: собрать typing events из Discourse (ежедневно 03:30 UTC)."""
    try:
        from helpers.discourse_typing_collector import collect_discourse_typing
        await collect_discourse_typing()
    except Exception as exc:
        logger.error("[discourse_typing_collect] unexpected error: %s", exc, exc_info=True)


async def _wakatime_typing_collect():
    """WP-327 Phase 4: собрать typing events из WakaTime (ежедневно 22:00 UTC)."""
    try:
        from helpers.wakatime_typing_collector import collect_wakatime_typing
        await collect_wakatime_typing()
    except Exception as exc:
        logger.error("[wakatime_typing_collect] unexpected error: %s", exc, exc_info=True)


async def _refresh_subscribers_snapshot():
    """WP-327 Этап 13: обновить daily snapshot подписчиков (tier >= T2) в rewards DB.

    Запускается в 01:00 UTC (04:00 МСК) ежедневно + при старте (+60s) как fallback.
    ON CONFLICT DO NOTHING → idempotent, повторный запуск безопасен.
    Fail-open не применяется здесь: это batch-job, не критический path.
    """
    try:
        from db.connection import get_pool, get_rewards_pool

        # Шаг 1: выбрать account_id (ory_id) подписчиков T2+ из основной БД бота
        main_pool = await get_pool()
        async with main_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT ory_id FROM public.users"
                " WHERE tier IN ('T2', 'T3', 'T4', 'T5') AND ory_id IS NOT NULL"
            )

        if not rows:
            logger.info("[SubscribersSnapshot] No T2+ users with ory_id found — snapshot skipped")
            return

        account_ids = [str(r["ory_id"]) for r in rows]

        # Шаг 2: upsert в rewards.subscribers_snapshot для CURRENT_DATE
        rewards_pool = await get_rewards_pool()
        async with rewards_pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO public.subscribers_snapshot (account_id, snapshot_date)
                VALUES ($1::uuid, CURRENT_DATE)
                ON CONFLICT (account_id, snapshot_date) DO NOTHING
                """,
                [(aid,) for aid in account_ids],
            )

            # Шаг 3: staleness check
            health = await conn.fetchrow(
                "SELECT status, today_count FROM public.v_subscribers_snapshot_health"
            )

            # Шаг 4: cleanup старых записей (> 3 дней)
            deleted_rows = await conn.fetch(
                "DELETE FROM public.subscribers_snapshot"
                " WHERE snapshot_date < CURRENT_DATE - 3 RETURNING account_id"
            )
            deleted = len(deleted_rows)

        status = health["status"] if health else "stale_critical"
        today_count = health["today_count"] if health else 0
        logger.info(
            "[SubscribersSnapshot] Refreshed: %d account_ids upserted, health=%s today_count=%d, cleanup=%s rows",
            len(account_ids), status, today_count, deleted,
        )

        if status != "ok":
            dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
            if dev_chat_id and _bot_token:
                bot = Bot(token=_bot_token)
                try:
                    await bot.send_message(
                        int(dev_chat_id),
                        f"⚠️ <b>[WP-327] subscribers_snapshot stale</b>\n"
                        f"status={status}, today_count={today_count}",
                        parse_mode="HTML",
                    )
                finally:
                    await bot.session.close()

    except Exception as exc:
        logger.error("[SubscribersSnapshot] refresh failed: %s", exc, exc_info=True)
