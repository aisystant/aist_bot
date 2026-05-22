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
)
from db.queries.users import moscow_now
from config import get_logger

logger = get_logger(__name__)

marathon_router = Router(name="marathon")

# ════════════════════════════════════════════════════════════════════
# Ф2.3 /marathon_start — регистрация + заполнение очереди на 14 дней
# ════════════════════════════════════════════════════════════════════

@marathon_router.message(Command("marathon_start"))
async def cmd_marathon_start(message: Message):
    """Старт марафона новичков: регистрация, заполнение очереди, приветствие."""
    chat_id = message.chat.id

    # Проверяем текущий прогресс
    progress = await get_or_create_progress(chat_id)
    current_status = progress.get("status", "registered")

    if current_status == "active":
        await message.answer(
            "🎉 Ты уже в марафоне!\n\n"
            f"📅 Текущий день: {progress.get('current_day', 0)}\n"
            "Используй /marathon_progress, чтобы узнать статус."
        )
        return

    if current_status == "completed":
        await message.answer(
            "✅ Ты уже завершил марафон!\n\n"
            "Если хочешь пройти снова — напиши в поддержку /support."
        )
        return

    # Активируем прогресс
    now = moscow_now()
    await update_progress(
        user_id=chat_id,
        status="active",
        started_at=now,
        current_day=0,
    )

    # Заполняем очередь на 14 дней (начиная с завтра, 04:00 MSK)
    from core.marathon_content import get_day_text

    base_time = datetime(now.year, now.month, now.day, 4, 0, 0, tzinfo=now.tzinfo)
    if now.hour >= 4:
        # Если уже после 04:00 — первый урок завтра
        base_time += timedelta(days=1)

    for day in range(1, 15):
        scheduled = base_time + timedelta(days=day - 1)
        content_texts = {
            'lesson': get_day_text(day, 'lesson'),
            'practice': get_day_text(day, 'practice'),
            'checkin': get_day_text(day, 'checkin'),
        }
        await enqueue_day_items(chat_id, day, scheduled, content_texts)

    logger.info(f"[Marathon] User {chat_id} started marathon. Queue filled 1-14 days.")

    # Приветственное сообщение (inline, без i18n для MVP — доработать в Ф1.3)
    await message.answer(
        "🚀 Добро пожаловать в марафон «Первые шаги в IWE»!\n\n"
        "📅 Формат: 14 дней, 3 сообщения в день\n"
        "Утром — теория (один короткий экран), днём — практика (одно конкретное действие), "
        "вечером — чек-ин (пара вопросов про день).\n\n"
        "🎯 Через 14 дней ты почувствуешь первую собранность: появится ритм, "
        "привычка думать методично — и понимание, что интеллектуальная работа поддаётся освоению.\n\n"
        "📋 Команды марафона:\n"
        "• /marathon_progress — мой прогресс\n"
        "• /marathon_stop — остановить марафон\n"
        "• /support — написать в поддержку\n\n"
        "Первый урок придёт завтра утром. До встречи! 👋"
    )


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
        lines.append(f"📅 Текущий день: {current_day} / 14")
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
