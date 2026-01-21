"""
UI для выбора режима работы бота.

Позволяет переключаться между:
- Марафон: 14-дневный структурированный курс
- Лента: бесконечное изучение по выбранным темам
"""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from config import get_logger, Mode, MarathonStatus, FeedStatus
from db.queries.users import get_intern, update_intern
from locales import t

logger = get_logger(__name__)

# Создаём роутер для выбора режима
mode_router = Router(name="mode_selector")


@mode_router.message(Command("mode"))
async def cmd_mode(message: Message):
    """Команда /mode - выбор режима работы"""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)

    if not intern:
        await message.answer(
            "Сначала пройдите регистрацию: /start"
        )
        return

    current_mode = intern.get('mode', Mode.MARATHON)
    marathon_status = intern.get('marathon_status', MarathonStatus.NOT_STARTED)
    feed_status = intern.get('feed_status', FeedStatus.NOT_STARTED)

    # Определяем текущий статус
    marathon_info = get_marathon_status_text(intern)
    feed_info = get_feed_status_text(intern)

    text = (
        "🎯 *Выберите режим обучения*\n\n"
        f"*Текущий режим:* {get_mode_name(current_mode)}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📚 *Марафон* — 14-дневный курс\n"
        f"{marathon_info}\n"
        "_Каждый день изучаете две темы: теория + практика_\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🌊 *Лента* — бесконечный режим\n"
        f"{feed_info}\n"
        "_ИИ предлагает темы, вы выбираете что изучать_\n"
    )

    # Кнопки выбора режима
    buttons = [
        [InlineKeyboardButton(
            text="📚 Марафон" + (" ✓" if current_mode == Mode.MARATHON else ""),
            callback_data="mode_marathon"
        )],
        [InlineKeyboardButton(
            text="🌊 Лента" + (" ✓" if current_mode == Mode.FEED else ""),
            callback_data="mode_feed"
        )],
    ]

    # Если оба режима активны, показываем статус "Оба"
    if current_mode == Mode.BOTH:
        buttons.append([InlineKeyboardButton(
            text="📚🌊 Оба режима ✓",
            callback_data="mode_both"
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@mode_router.callback_query(F.data == "mode_marathon")
async def select_marathon(callback: CallbackQuery):
    """Выбор режима Марафон"""
    try:
        chat_id = callback.message.chat.id
        intern = await get_intern(chat_id)

        marathon_status = intern.get('marathon_status', MarathonStatus.NOT_STARTED)
        has_progress = len(intern.get('completed_topics', [])) > 0 or intern.get('current_topic_index', 0) > 0

        # Если марафон был на паузе - возобновляем
        if marathon_status == MarathonStatus.PAUSED:
            await update_intern(chat_id,
                mode=Mode.MARATHON,
                marathon_status=MarathonStatus.ACTIVE,
            )
            await callback.message.edit_text(
                "✅ *Режим Марафон возобновлён!*\n\n"
                "Используйте /learn для продолжения обучения.",
                parse_mode="Markdown"
            )
        elif marathon_status == MarathonStatus.COMPLETED:
            await callback.message.edit_text(
                "🎉 *Марафон завершён!*\n\n"
                "Вы уже прошли 14-дневный курс.\n"
                "Рекомендуем перейти в режим Лента: /feed",
                parse_mode="Markdown"
            )
        elif marathon_status == MarathonStatus.NOT_STARTED and not has_progress:
            # Реально новый пользователь
            await update_intern(chat_id,
                mode=Mode.MARATHON,
                marathon_status=MarathonStatus.ACTIVE,
            )
            await callback.message.edit_text(
                "✅ *Режим Марафон активирован!*\n\n"
                "Используйте /learn для начала обучения.",
                parse_mode="Markdown"
            )
        else:
            # Активен или legacy пользователь с прогрессом
            await update_intern(chat_id,
                mode=Mode.MARATHON,
                marathon_status=MarathonStatus.ACTIVE,
            )
            await callback.message.edit_text(
                "✅ *Режим Марафон*\n\n"
                "Используйте /learn для продолжения обучения.",
                parse_mode="Markdown"
            )

        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка в select_marathon: {e}")
        await callback.answer("Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@mode_router.callback_query(F.data == "mode_feed")
async def select_feed(callback: CallbackQuery):
    """Выбор режима Лента"""
    try:
        chat_id = callback.message.chat.id
        intern = await get_intern(chat_id)

        current_mode = intern.get('mode', Mode.MARATHON)
        marathon_status = intern.get('marathon_status', MarathonStatus.NOT_STARTED)
        lang = intern.get('language', 'ru') or 'ru'

        # Для legacy: проверяем реальный прогресс марафона
        has_marathon_progress = len(intern.get('completed_topics', [])) > 0 or intern.get('current_topic_index', 0) > 0

        # Получаем настройки пользователя
        settings_text = get_user_settings_text(intern)

        # Проверяем, есть ли активная неделя
        from .feed.engine import FeedEngine
        engine = FeedEngine(chat_id)
        status = await engine.get_status()
        has_active_week = status.get('has_week') and status.get('week_status') == 'active'

        # Формируем текст сообщения
        text = "✅ *Режим Лента активирован!*\n\n"
        text += f"*Ваши настройки:*\n{settings_text}\n"

        if has_active_week:
            # Есть активная неделя — показываем прогресс
            topics = status.get('topics', [])
            current_day = status.get('current_day', 1)
            text += f"\n{t('feed.week_progress', lang, current=current_day, total=len(topics))}"
            if current_day <= len(topics):
                text += f"\n📖 Сегодня: *{topics[current_day - 1]}*"

        # Если был активный марафон - ставим на паузу
        if (marathon_status == MarathonStatus.ACTIVE or
            (marathon_status == MarathonStatus.NOT_STARTED and has_marathon_progress)):
            await update_intern(chat_id,
                mode=Mode.FEED,
                marathon_status=MarathonStatus.PAUSED,
                feed_status=FeedStatus.ACTIVE,
            )
            text += "\n\n_Марафон на паузе. Вернуться: /mode_"
        else:
            await update_intern(chat_id,
                mode=Mode.FEED,
                feed_status=FeedStatus.ACTIVE,
            )

        # Кнопки в зависимости от наличия активной недели
        if has_active_week:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"📖 {t('buttons.get_digest', lang)}",
                    callback_data="feed_get_digest"
                )],
                [InlineKeyboardButton(
                    text=f"📋 {t('buttons.topics_menu', lang)}",
                    callback_data="feed_topics_menu"
                )]
            ])
        else:
            # Нет активной недели — кнопка для выбора тем
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"📚 {t('buttons.select_topics', lang)}",
                    callback_data="feed_start_topics"
                )]
            ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в select_feed: {e}\n{traceback.format_exc()}")
        await callback.answer("Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


def get_mode_name(mode: str) -> str:
    """Возвращает название режима"""
    names = {
        Mode.MARATHON: "📚 Марафон",
        Mode.FEED: "🌊 Лента",
        Mode.BOTH: "📚🌊 Оба режима",
    }
    return names.get(mode, "Не выбран")


def get_marathon_status_text(intern: dict) -> str:
    """Возвращает текст статуса марафона"""
    status = intern.get('marathon_status', MarathonStatus.NOT_STARTED)
    completed = intern.get('completed_topics', [])
    topic_index = intern.get('current_topic_index', 0)

    # Вычисляем день (каждый день = 2 темы)
    day = (topic_index // 2) + 1 if topic_index > 0 else 0

    # Для legacy пользователей: если есть прогресс, но статус not_started — считаем активным
    has_progress = len(completed) > 0 or topic_index > 0

    if status == MarathonStatus.COMPLETED or (has_progress and day > 14):
        return "✅ Завершён"
    elif status == MarathonStatus.ACTIVE or (status == MarathonStatus.NOT_STARTED and has_progress):
        return f"🟢 Активен (день {day}/14, пройдено {len(completed)} тем)"
    elif status == MarathonStatus.PAUSED:
        return f"⏸️ На паузе (день {day}/14)"
    elif status == MarathonStatus.NOT_STARTED:
        return "⚪ Не начат"
    return "⚪ Статус неизвестен"


def get_feed_status_text(intern: dict) -> str:
    """Возвращает текст статуса ленты"""
    status = intern.get('feed_status', FeedStatus.NOT_STARTED)
    active_days = intern.get('active_days_total', 0)

    if status == FeedStatus.NOT_STARTED:
        return "⚪ Не начата"
    elif status == FeedStatus.ACTIVE:
        return f"🟢 Активна (активных дней: {active_days})"
    elif status == FeedStatus.PAUSED:
        return f"⏸️ На паузе (активных дней: {active_days})"
    return "⚪ Статус неизвестен"


def get_complexity_name(level: int) -> str:
    """Возвращает название уровня сложности"""
    names = {
        1: "Начальный",
        2: "Базовый",
        3: "Средний",
        4: "Продвинутый",
        5: "Экспертный",
    }
    return names.get(level, f"Уровень {level}")


def get_user_settings_text(intern: dict) -> str:
    """Формирует текст с настройками пользователя для Ленты"""
    schedule_time = intern.get('schedule_time', '09:00')
    study_duration = intern.get('study_duration', 15)
    complexity = intern.get('complexity_level') or intern.get('bloom_level', 1)

    return (
        f"⏰ Время: {schedule_time}\n"
        f"📖 На чтение: {study_duration} мин\n"
        f"📊 Сложность: {get_complexity_name(complexity)}"
    )
