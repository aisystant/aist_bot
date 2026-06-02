"""
Хендлеры нового марафона для новичков (WP-330).

Прямая работа с learning.marathon_queue / marathon_progress / marathon_state.
Не использует legacy aiogram FSM — состояние хранится в БД.
"""

import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from db.queries.marathon_newcomer import (
    get_or_create_progress,
    update_progress,
    enqueue_day_items,
    save_checkin,
    clear_marathon_queue,
    clear_marathon_state,
    get_sent_checkins_count,
    get_total_checkins_count,
)
from db.queries.users import moscow_now, update_intern
from config import get_logger

logger = get_logger(__name__)

marathon_router = Router(name="marathon")

# Если старт до этого часа (МСК) — первый урок отправляется немедленно. DP.SC.157.
MARATHON_SAME_DAY_CUTOFF_HOUR = 18


async def start_marathon_flow(user_id: int, reply_msg, schedule_time: str = "04:00") -> None:
    """Запуск марафона: регистрация, заполнение очереди, первый урок. DP.SC.157."""
    progress = await get_or_create_progress(user_id)
    current_status = progress.get("status", "registered")

    if current_status == "active":
        await reply_msg.answer(
            "🎉 Ты уже в марафоне!\n\n"
            f"📅 День марафона: {progress.get('current_day', 0)} / 14\n\n"
            "📋 Команды:\n"
            "• /marathon_progress — статус и прогресс\n"
            "• /marathon_stop — остановить марафон\n"
            "• /profile — изменить время и уровень сложности\n"
            "• /support — поддержка"
        )
        return

    if current_status == "completed":
        await reply_msg.answer(
            "✅ Ты уже завершил марафон!\n\n"
            "Если хочешь пройти снова — напиши в поддержку /support."
        )
        return

    now = moscow_now()
    # WP-330 Ф8.2: чистим marathon_state перед стартом — иначе унаследуем
    # checkin-записи прошлых тестов, и первый реальный чек-ин не инкрементирует
    # current_day (existing != None в callback_marathon_checkin). total_checkins
    # derived из marathon_state — отдельно не инкрементируется.
    await clear_marathon_state(user_id)
    await update_progress(
        user_id=user_id,
        status="active",
        started_at=now,
        current_day=1,
    )
    await update_intern(user_id, marathon_status="active", onboarding_completed=True)

    from core.marathon_content import get_day_text
    from db.queries import get_intern

    # WP-330 С9a: подаём intern в get_day_text для routing 4 версий контента
    intern_for_routing = await get_intern(user_id)

    # DP.SC.157: <18:00 МСК → день 1 немедленно; ≥18:00 → завтра 04:00 МСК
    sched_h, sched_m = map(int, schedule_time.split(":"))
    tomorrow_sched = datetime(now.year, now.month, now.day, sched_h, sched_m, 0, tzinfo=now.tzinfo) + timedelta(days=1)
    if now.hour < MARATHON_SAME_DAY_CUTOFF_HOUR:
        day1_time = now + timedelta(minutes=1)
        next_day_base = tomorrow_sched
        first_lesson_today = True
    else:
        day1_time = tomorrow_sched
        next_day_base = tomorrow_sched + timedelta(days=1)
        first_lesson_today = False

    for day in range(1, 15):
        scheduled = day1_time if day == 1 else next_day_base + timedelta(days=day - 2)
        # WP-330 B2: delivery-time rendering — не материализуем lesson_practice при enqueue.
        # Scheduler рендерит текст из свежего intern непосредственно перед отправкой.
        # checkin не routable — материализуем сразу.
        content_texts = {
            'lesson_practice': None,
            'checkin': get_day_text(day, 'checkin'),
        }
        await enqueue_day_items(user_id, day, scheduled, content_texts)

    logger.info("[Marathon] user_id=%s started. Queue 1-14 filled. immediate=%s", user_id, first_lesson_today)

    first_lesson_note = (
        f"Первый урок придёт через минуту — приготовься!\nСо дня 2 уроки приходят ежедневно в {schedule_time} МСК."
        if first_lesson_today
        else f"Первый урок и все последующие приходят ежедневно в {schedule_time} МСК."
    )
    await reply_msg.answer(
        "🚀 Добро пожаловать в марафон «Первые шаги в IWE»!\n\n"
        "14 дней × 20 мин/день, можно ставить паузу.\n\n"
        "Утром — теория и практика (один конкретный шаг), вечером — чек-ин.\n\n"
        f"📅 {first_lesson_note}\n\n"
        "📋 Команды:\n"
        "• /marathon_progress — прогресс\n"
        "• /marathon_stop — поставить на паузу\n"
        "• /profile — изменить время и уровень сложности\n"
        "• /support — поддержка"
    )


# ════════════════════════════════════════════════════════════════════
# Ф2.3 /marathon_start — регистрация + заполнение очереди на 14 дней
# ════════════════════════════════════════════════════════════════════

@marathon_router.message(Command("marathon_start"))
async def cmd_marathon_start(message: Message):
    """Старт марафона новичков: регистрация, заполнение очереди, приветствие."""
    from db.queries import get_intern
    intern = await get_intern(message.chat.id)
    sched = (intern or {}).get("schedule_time") or "04:00"
    await start_marathon_flow(message.chat.id, message, schedule_time=sched)


# ════════════════════════════════════════════════════════════════════
# Ф2.5 /marathon_progress — статус и текущий день
# ════════════════════════════════════════════════════════════════════

@marathon_router.message(Command("marathon_progress"))
async def cmd_marathon_progress(message: Message):
    """Показать текущий прогресс участника марафона."""
    chat_id = message.chat.id
    progress = await get_or_create_progress(chat_id)

    status = progress.get("status", "registered")
    current_day = progress.get("current_day", 0)
    total_checkins = await get_total_checkins_count(chat_id)
    started_at = progress.get("started_at")

    status_emoji = {
        "registered": "📝",
        "active": "🏃",
        "paused": "⏸",
        "completed": "✅",
        "dropped": "🚫",
    }.get(status, "❓")

    lines = [
        f"{status_emoji} Статус: {status}",
    ]

    if status == "active":
        display_day = current_day if current_day > 0 else 1
        # WP-330 B1: считаем пропущенные от отправленных чек-инов, не от current_day.
        # current_day — это номер последнего задеплоенного дня, а не ожидаемых чек-инов.
        sent_checkins = await get_sent_checkins_count(chat_id)
        missed_checkins = max(0, sent_checkins - total_checkins)
        lines.append(f"📅 День марафона: {display_day} / 14")
        lines.append(f"🌙 Чек-инов: {total_checkins}")
        lines.append(f"❌ Пропущено чек-инов: {missed_checkins}")
        if started_at:
            started_str = started_at.strftime("%d.%m.%Y")
            lines.append(f"🚀 Старт: {started_str}")
    elif status == "registered":
        lines.append("\nНачни марафон командой /marathon_start")
    elif status == "completed":
        lines.append("\nПоздравляем с завершением! 🎉")

    await message.answer("\n".join(lines))


# ════════════════════════════════════════════════════════════════════
# Ф3.1 / Ф3.2 — Обработка чек-ина: кнопки 😵/🧱/🔁
# ════════════════════════════════════════════════════════════════════

@marathon_router.callback_query(F.data.startswith("marathon_checkin:"))
async def callback_marathon_checkin(callback: CallbackQuery):
    """Сохранить состояние чек-ина, продвинуть current_day, подтвердить выбор."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка формата данных", show_alert=True)
        return

    _, state, day_str = parts
    try:
        day = int(day_str)
    except ValueError:
        await callback.answer("Ошибка формата дня", show_alert=True)
        return

    user_id = callback.from_user.id

    # Атомарный UPSERT — возвращает True если это первый чек-ин за день.
    # Исключает TOCTOU при двойном тапе: решение о counting принимается на уровне DB.
    is_first_checkin = await save_checkin(user_id, day, state)
    is_completed = False

    # Инкремент прогресса только при первом чек-ине за день
    if is_first_checkin:
        progress = await get_or_create_progress(user_id)
        current_day = progress.get("current_day", 0)

        new_day = max(current_day, day)
        new_status = None
        if day >= 14:
            new_status = "completed"

        await update_progress(
            user_id=user_id,
            current_day=new_day,
            status=new_status,
        )

        if new_status == "completed":
            await update_intern(user_id, marathon_status="completed")
            is_completed = True

        logger.info(
            f"[MarathonCheckin] User {user_id} day {day} state={state} "
            f"current_day {current_day}→{new_day}"
        )
    else:
        logger.info(f"[MarathonCheckin] User {user_id} updated day {day} state={state} (already checked in)")

    state_labels = {
        "chaos": "😵 Хаос",
        "stuck": "🧱 Тупик",
        "turn": "🔁 Поворот",
    }
    label = state_labels.get(state, state)

    await callback.answer(f"Записано: {label}", show_alert=False)

    # Убираем кнопки и показываем выбор
    original_text = callback.message.text or callback.message.caption or ""
    footer = "" if is_completed else "\n\n📋 /marathon_progress — прогресс | /marathon_stop — пауза"
    await callback.message.edit_text(
        f"{original_text}\n\n✅ Твой выбор: {label}{footer}",
        reply_markup=None,
    )

    if is_completed:
        await callback.message.answer(
            "🎉 Поздравляем с завершением марафона «Первые шаги в IWE»!\n\n"
            "14 дней пройдено — ты молодец! Ты освоил базовые инструменты "
            "системного развития.\n\n"
            "📋 /marathon_progress — посмотреть статистику\n"
            "• /support — вопросы и поддержка"
        )


# ════════════════════════════════════════════════════════════════════
# WP-330 Ф10.C — Callback «✏️ Перейти к практике»
# ════════════════════════════════════════════════════════════════════

@marathon_router.callback_query(F.data.startswith("marathon_practice:"))
async def callback_marathon_practice(callback: CallbackQuery):
    """Доставить practice по нажатию кнопки. Idempotent через domain_event."""
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer("Ошибка формата данных", show_alert=True)
        return
    try:
        day = int(parts[1])
    except ValueError:
        await callback.answer("Ошибка формата дня", show_alert=True)
        return

    user_id = callback.from_user.id

    from db.queries.notifications import was_notification_sent, try_insert_notification
    idempotency_key = f"marathon_practice:{user_id}:{day}"
    # Сначала проверка без записи (избегаем dedup-lock при сбое доставки → retry возможен)
    if await was_notification_sent(idempotency_key):
        await callback.answer("Практика дня уже доставлена ✅", show_alert=False)
        return

    from core.marathon_content import get_day_text
    from db.queries import get_intern
    # WP-330 С9a: routing по профилю → short_simple/short_complex/long_simple/long_complex
    intern_for_routing = await get_intern(user_id)
    practice = (
        get_day_text(day, 'practice', intern=intern_for_routing)
        or get_day_text(day, 'practice')
    )
    if not practice:
        await callback.answer("Практика для этого дня недоступна", show_alert=True)
        logger.warning(f"[MarathonPractice] No practice content for day {day} (user {user_id})")
        return

    practice_text = practice
    if len(practice_text) > 4000:
        practice_text = practice_text[:3990] + "\n\n…"

    try:
        await callback.message.answer(practice_text, parse_mode="Markdown")
        # Запись dedup только после успешной доставки (sent-before-log)
        await try_insert_notification(user_id, "marathon_practice", idempotency_key)
        # Убираем кнопку у lesson-сообщения, чтобы избежать визуального дребезга
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass  # некритично если не получилось снять inline-кнопку
        await callback.answer()
        logger.info(f"[MarathonPractice] Sent practice day {day} to {user_id}")
    except Exception as e:
        logger.error(
            f"[MarathonPractice] Failed to send practice day {day} to {user_id}: "
            f"{type(e).__name__}: {e}"
        )
        await callback.answer("Не удалось отправить практику. Попробуй ещё раз позже.", show_alert=True)


# ════════════════════════════════════════════════════════════════════
# Ф2.8 /marathon_stop — выход из марафона
# ════════════════════════════════════════════════════════════════════

@marathon_router.message(Command("marathon_stop"))
async def cmd_marathon_stop(message: Message):
    """Остановить марафон: очистить очередь, статус → dropped."""
    chat_id = message.chat.id
    progress = await get_or_create_progress(chat_id)

    if progress.get("status") != "active":
        await message.answer(
            "ℹ️ У тебя нет активного марафона.\n"
            "Начни командой /marathon_start"
        )
        return

    # Очищаем pending-записи из очереди
    await clear_marathon_queue(chat_id)
    # WP-330 Ф8.2: чистим marathon_state — иначе при перезапуске новые чек-ины
    # не инкрементируют current_day (existing != None блокирует update_progress).
    await clear_marathon_state(chat_id)

    # Обновляем статус
    await update_progress(
        user_id=chat_id,
        status="dropped",
    )
    await update_intern(chat_id, marathon_status="not_started")

    logger.info(f"[Marathon] User {chat_id} stopped marathon.")
    await message.answer(
        "🛑 Марафон остановлен.\n\n"
        "Если захочешь вернуться — /marathon_start (начнёшь сначала).\n\n"
        "📋 /profile — настройки | /support — поддержка"
    )
