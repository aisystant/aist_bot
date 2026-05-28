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
    get_checkin_for_day,
    clear_marathon_queue,
    clear_marathon_state,
)
from db.queries.users import moscow_now, update_intern
from config import get_logger

logger = get_logger(__name__)

marathon_router = Router(name="marathon")

# Если старт до этого часа (МСК) — первый урок отправляется немедленно. DP.SC.157.
MARATHON_SAME_DAY_CUTOFF_HOUR = 18


async def start_marathon_flow(user_id: int, reply_msg) -> None:
    """Запуск марафона: регистрация, заполнение очереди, первый урок. DP.SC.157."""
    progress = await get_or_create_progress(user_id)
    current_status = progress.get("status", "registered")

    if current_status == "active":
        await reply_msg.answer(
            "🎉 Ты уже в марафоне!\n\n"
            f"📅 Текущий день: {progress.get('current_day', 0)}\n\n"
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
    # current_day/total_checkins (existing != None в callback_marathon_checkin).
    await clear_marathon_state(user_id)
    await update_progress(
        user_id=user_id,
        status="active",
        started_at=now,
        current_day=1,
        total_checkins=0,
        missed_days=0,
    )
    await update_intern(user_id, marathon_status="active", onboarding_completed=True)

    from core.marathon_content import get_day_text

    # DP.SC.157: <18:00 МСК → день 1 немедленно; ≥18:00 → завтра 04:00 МСК
    tomorrow_0400 = datetime(now.year, now.month, now.day, 4, 0, 0, tzinfo=now.tzinfo) + timedelta(days=1)
    if now.hour < MARATHON_SAME_DAY_CUTOFF_HOUR:
        day1_time = now + timedelta(minutes=1)
        next_day_base = tomorrow_0400
        first_lesson_today = True
    else:
        day1_time = tomorrow_0400
        next_day_base = tomorrow_0400 + timedelta(days=1)
        first_lesson_today = False

    for day in range(1, 15):
        scheduled = day1_time if day == 1 else next_day_base + timedelta(days=day - 2)
        lesson = get_day_text(day, 'lesson')
        practice = get_day_text(day, 'practice')
        lesson_practice = f"{lesson}\n\n{practice}" if lesson and practice else (lesson or practice or "")
        content_texts = {
            'lesson_practice': lesson_practice,
            'checkin': get_day_text(day, 'checkin'),
        }
        await enqueue_day_items(user_id, day, scheduled, content_texts)

    logger.info("[Marathon] user_id=%s started. Queue 1-14 filled. immediate=%s", user_id, first_lesson_today)

    first_lesson_note = (
        "Первый урок придёт через минуту — приготовься!" if first_lesson_today
        else "Первый урок придёт завтра в 04:00 МСК."
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
    await start_marathon_flow(message.chat.id, message)


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
    total_checkins = progress.get("total_checkins", 0)
    missed_days = progress.get("missed_days", 0)
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
        lines.append(f"📅 Текущий день: {display_day} / 14")
        lines.append(f"🌙 Чек-инов: {total_checkins}")
        lines.append(f"❌ Пропусков: {missed_days}")
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

    # Проверяем, не чекинился ли уже за этот день
    existing = await get_checkin_for_day(user_id, day)

    # Сохраняем (или обновляем) состояние
    await save_checkin(user_id, day, state)

    # Инкремент прогресса только при первом чек-ине за день
    if not existing:
        progress = await get_or_create_progress(user_id)
        current_day = progress.get("current_day", 0)
        total_checkins = progress.get("total_checkins", 0)

        new_day = max(current_day, day)
        new_total = total_checkins + 1
        new_status = None
        if day >= 14:
            new_status = "completed"

        await update_progress(
            user_id=user_id,
            current_day=new_day,
            total_checkins=new_total,
            status=new_status,
        )

        logger.info(
            f"[MarathonCheckin] User {user_id} day {day} state={state} "
            f"current_day {current_day}→{new_day} total_checkins {total_checkins}→{new_total}"
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
    await callback.message.edit_text(
        f"{original_text}\n\n✅ Твой выбор: {label}",
        reply_markup=None,
    )


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
    # не инкрементируют counters (existing != None блокирует update_progress).
    await clear_marathon_state(chat_id)

    # Обновляем статус
    await update_progress(
        user_id=chat_id,
        status="dropped",
    )

    logger.info(f"[Marathon] User {chat_id} stopped marathon.")
    await message.answer(
        "🛑 Марафон остановлен.\n\n"
        "Если захочешь вернуться — напиши /marathon_start. "
        "Ты начнёшь сначала (очередь будет пересоздана)."
    )
