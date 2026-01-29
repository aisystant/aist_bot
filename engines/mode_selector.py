"""
UI для выбора режима работы бота.

Позволяет переключаться между:
- Марафон: 14-дневный структурированный курс
- Лента: бесконечное изучение по выбранным темам
"""

import re
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import get_logger, Mode, MarathonStatus, FeedStatus
from db.queries.users import get_intern, update_intern
from i18n import t


class MarathonSettingsStates(StatesGroup):
    """Состояния для настроек марафона"""
    waiting_for_time = State()  # Ожидание ввода времени напоминаний

logger = get_logger(__name__)

# Создаём роутер для выбора режима
mode_router = Router(name="mode_selector")


@mode_router.message(Command("mode"))
async def cmd_mode(message: Message):
    """Команда /mode - выбор режима работы"""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)

    if not intern:
        await message.answer(t('profile.not_found', 'ru'))
        return

    lang = intern.get('language', 'ru') or 'ru'
    current_mode = intern.get('mode', Mode.MARATHON)
    marathon_status = intern.get('marathon_status', MarathonStatus.NOT_STARTED)
    feed_status = intern.get('feed_status', FeedStatus.NOT_STARTED)

    # Определяем текущий статус
    marathon_info = get_marathon_status_text(intern, lang)
    feed_info = get_feed_status_text(intern, lang)

    text = (
        f"{t('modes.select_mode_title', lang)}\n\n"
        f"{t('modes.current_mode_label', lang)} {get_mode_name(current_mode, lang)}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{t('modes.marathon_full_desc', lang)}\n"
        f"{marathon_info}\n"
        f"{t('modes.marathon_daily', lang)}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{t('modes.feed_full_desc', lang)}\n"
        f"{feed_info}\n"
        f"{t('modes.feed_daily', lang)}\n"
    )

    # Кнопки выбора режима
    buttons = [
        [InlineKeyboardButton(
            text=t('modes.marathon_button', lang) + (" ✓" if current_mode == Mode.MARATHON else ""),
            callback_data="mode_marathon"
        )],
        [InlineKeyboardButton(
            text=t('modes.feed_button', lang) + (" ✓" if current_mode == Mode.FEED else ""),
            callback_data="mode_feed"
        )],
    ]

    # Если оба режима активны, показываем статус "Оба"
    if current_mode == Mode.BOTH:
        buttons.append([InlineKeyboardButton(
            text=t('modes.both_modes', lang),
            callback_data="mode_both"
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@mode_router.callback_query(F.data == "mode_marathon")
async def select_marathon(callback: CallbackQuery):
    """Выбор режима Марафон — показывает статус и настройки"""
    try:
        chat_id = callback.message.chat.id
        intern = await get_intern(chat_id)

        marathon_status = intern.get('marathon_status', MarathonStatus.NOT_STARTED)
        feed_status = intern.get('feed_status', FeedStatus.NOT_STARTED)
        has_progress = len(intern.get('completed_topics', [])) > 0 or intern.get('current_topic_index', 0) > 0

        # Если была активная Лента - ставим на паузу
        if feed_status == FeedStatus.ACTIVE:
            await update_intern(chat_id,
                mode=Mode.MARATHON,
                marathon_status=MarathonStatus.ACTIVE if marathon_status != MarathonStatus.COMPLETED else MarathonStatus.COMPLETED,
                feed_status=FeedStatus.PAUSED,
            )
            feed_paused = True
        else:
            await update_intern(chat_id,
                mode=Mode.MARATHON,
                marathon_status=MarathonStatus.ACTIVE if marathon_status != MarathonStatus.COMPLETED else MarathonStatus.COMPLETED,
            )
            feed_paused = False

        # Обновляем intern после изменений
        intern = await get_intern(chat_id)

        # Показываем сообщение в стиле Ленты
        await show_marathon_activated(callback.message, intern, feed_paused, edit=True)
        await callback.answer()

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в select_marathon: {e}\n{traceback.format_exc()}")
        await callback.answer(t('modes.error_occurred', 'ru'), show_alert=True)


async def show_marathon_activated(message, intern: dict, feed_paused: bool = False, edit: bool = False):
    """Показывает сообщение об активации Марафона в стиле Ленты"""
    from db.queries.users import moscow_today

    lang = intern.get('language', 'ru') or 'ru'

    # Настройки
    schedule_time = intern.get('schedule_time', '09:00')
    schedule_time_2 = intern.get('schedule_time_2')
    study_duration = intern.get('study_duration', 15)
    bloom_level = intern.get('bloom_level', 1)
    complexity_text = get_complexity_name(bloom_level, lang)

    # Прогресс
    completed = len(intern.get('completed_topics', []))
    start_date = intern.get('marathon_start_date')
    today = moscow_today()

    if start_date:
        if hasattr(start_date, 'date'):
            start_date = start_date.date()
        days_passed = (today - start_date).days
        marathon_day = min(days_passed + 1, 14)
    else:
        marathon_day = 1

    # Формируем текст
    text = f"{t('modes.marathon_activated', lang)}\n\n"
    text += f"{t('modes.day_progress', lang, day=marathon_day, completed=completed)}\n\n"
    text += f"{t('modes.your_settings', lang)}\n"
    text += f"{t('modes.time_label', lang)} {schedule_time}\n"
    text += f"{t('modes.duration_label', lang)} {study_duration} {t('modes.min_suffix', lang)}\n"
    text += f"{t('modes.complexity_label', lang)} {complexity_text}\n"

    if schedule_time_2:
        text += f"{t('modes.extra_reminder', lang)} {schedule_time_2}\n"

    if feed_paused:
        text += f"\n{t('modes.feed_paused', lang)}"

    # Кнопки
    buttons = [
        [InlineKeyboardButton(text=f"📚 {t('buttons.continue_learning', lang)}", callback_data="learn")],
        [InlineKeyboardButton(text=t('buttons.update_profile', lang), callback_data="marathon_go_update")],
        [InlineKeyboardButton(text=t('buttons.reminders', lang), callback_data="marathon_reminders_input")],
        [InlineKeyboardButton(text=t('buttons.reset_marathon', lang), callback_data="marathon_reset_confirm")],
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


async def show_marathon_settings(message, intern: dict, edit: bool = False):
    """Показывает меню настроек марафона (устаревшая функция, используется show_marathon_activated)"""
    # Перенаправляем на новую функцию
    await show_marathon_activated(message, intern, feed_paused=False, edit=edit)


async def show_feed_activated(message, intern: dict, marathon_paused: bool = False, edit: bool = False):
    """Показывает сообщение об активации Ленты"""
    chat_id = message.chat.id if hasattr(message, 'chat') else intern.get('chat_id')
    lang = intern.get('language', 'ru') or 'ru'

    # Получаем настройки
    settings_text = get_user_settings_text(intern, lang)

    # Проверяем, есть ли активная неделя
    from .feed.engine import FeedEngine
    engine = FeedEngine(chat_id)
    status = await engine.get_status()
    has_active_week = status.get('has_week') and status.get('week_status') == 'active'

    # Формируем текст
    text = f"{t('modes.feed_activated', lang)}\n\n"
    text += f"{t('modes.your_settings', lang)}\n{settings_text}\n"

    if has_active_week:
        topics = status.get('topics', [])
        if topics:
            text += f"\n{t('feed.your_topics_label', lang)}\n"
            for i, topic in enumerate(topics, 1):
                text += f"{i}. {topic}\n"

    if marathon_paused:
        text += f"\n{t('modes.marathon_paused', lang)}"

    # Кнопки
    buttons = []

    if has_active_week:
        buttons.append([InlineKeyboardButton(
            text=f"📖 {t('buttons.get_digest', lang)}",
            callback_data="feed_get_digest"
        )])
        buttons.append([InlineKeyboardButton(
            text=f"📋 {t('buttons.topics_menu', lang)}",
            callback_data="feed_topics_menu"
        )])
    else:
        buttons.append([InlineKeyboardButton(
            text=f"📚 {t('buttons.select_topics', lang)}",
            callback_data="feed_start_topics"
        )])

    buttons.append([InlineKeyboardButton(text=t('buttons.update_profile', lang), callback_data="feed_go_update")])
    buttons.append([InlineKeyboardButton(text=t('buttons.reminders', lang), callback_data="feed_reminders_input")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@mode_router.callback_query(F.data == "marathon_continue")
async def marathon_continue(callback: CallbackQuery):
    """Продолжить марафон (legacy)"""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    await callback.message.edit_text(
        f"✅ *{t('modes.marathon_mode', lang)}*\n\n"
        f"{t('modes.use_learn', lang)}",
        parse_mode="Markdown"
    )
    await callback.answer()


@mode_router.callback_query(F.data == "marathon_back_to_mode")
async def marathon_back_to_mode(callback: CallbackQuery):
    """Назад к выбору режима"""
    await cmd_mode(callback.message)
    await callback.answer()


# ==================== НАСТРОЙКА ДАТЫ СТАРТА ====================

@mode_router.callback_query(F.data == "marathon_set_date")
async def marathon_set_date(callback: CallbackQuery):
    """Меню настройки даты старта"""
    from db.queries.users import moscow_today
    from datetime import timedelta

    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)

    start_date = intern.get('marathon_start_date')
    today = moscow_today()

    lang = intern.get('language', 'ru') or 'ru'

    if start_date:
        if hasattr(start_date, 'date'):
            start_date = start_date.date()
        days_passed = (today - start_date).days
        marathon_day = min(days_passed + 1, 14)
        current_date_str = start_date.strftime('%d.%m.%Y')
    else:
        marathon_day = 0
        current_date_str = t('modes.start_date_not_set', lang)

    completed = len(intern.get('completed_topics', []))

    text = f"🗓 *{t('modes.start_date_title', lang)}*\n\n"
    text += f"{t('modes.start_date_current', lang)}: {current_date_str}"
    if start_date:
        text += f" ({t('modes.day_label', lang)} {marathon_day})"
    text += "\n\n"

    # Кнопки
    buttons = []

    # Только даты вперёд
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    buttons.append([InlineKeyboardButton(
        text=f"📅 {t('modes.tomorrow', lang)} ({tomorrow.strftime('%d.%m')})",
        callback_data="marathon_date_tomorrow"
    )])
    buttons.append([InlineKeyboardButton(
        text=f"📅 {t('modes.day_after_tomorrow', lang)} ({day_after.strftime('%d.%m')})",
        callback_data="marathon_date_day_after"
    )])

    # Кнопка сброса (если есть прогресс)
    if completed > 0:
        buttons.append([InlineKeyboardButton(
            text=t('buttons.reset_marathon', lang),
            callback_data="marathon_reset_confirm"
        )])

    buttons.append([InlineKeyboardButton(
        text=t('buttons.back', lang),
        callback_data="marathon_settings_back"
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()


@mode_router.callback_query(F.data == "marathon_date_tomorrow")
async def marathon_date_tomorrow(callback: CallbackQuery):
    """Установить дату старта на завтра"""
    from db.queries.users import moscow_today
    from datetime import timedelta

    today = moscow_today()
    new_date = today + timedelta(days=1)

    await update_intern(callback.message.chat.id, marathon_start_date=new_date)
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    await callback.answer(t('modes.start_date_set', lang, date=new_date.strftime('%d.%m.%Y')))

    # Возвращаемся к настройкам
    intern = await get_intern(callback.message.chat.id)
    await show_marathon_settings(callback.message, intern, edit=True)


@mode_router.callback_query(F.data == "marathon_date_day_after")
async def marathon_date_day_after(callback: CallbackQuery):
    """Установить дату старта на послезавтра"""
    from db.queries.users import moscow_today
    from datetime import timedelta

    today = moscow_today()
    new_date = today + timedelta(days=2)

    await update_intern(callback.message.chat.id, marathon_start_date=new_date)
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    await callback.answer(t('modes.start_date_set', lang, date=new_date.strftime('%d.%m.%Y')))

    # Возвращаемся к настройкам
    intern = await get_intern(callback.message.chat.id)
    await show_marathon_settings(callback.message, intern, edit=True)


# ==================== СБРОС МАРАФОНА ====================

@mode_router.callback_query(F.data == "marathon_reset_confirm")
async def marathon_reset_confirm(callback: CallbackQuery):
    """Подтверждение сброса марафона"""
    try:
        chat_id = callback.message.chat.id
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

        completed = len(intern.get('completed_topics', []))

        text = f"⚠️ *{t('modes.reset_marathon_title', lang)}*\n\n"
        text += f"{t('modes.will_be_reset', lang)}:\n"
        text += f"• {completed} {t('modes.topics_passed', lang)}\n"
        text += f"• {t('modes.all_progress', lang)}\n\n"
        text += f"_{t('modes.feed_stats_kept', lang)}_"

        buttons = [
            [
                InlineKeyboardButton(text=f"🔄 {t('modes.yes_reset', lang)}", callback_data="marathon_reset_do"),
                InlineKeyboardButton(text=f"❌ {t('modes.cancel', lang)}", callback_data="marathon_settings_back")
            ]
        ]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в marathon_reset_confirm: {e}\n{traceback.format_exc()}")
        await callback.answer(t('modes.error_occurred', lang if 'lang' in dir() else 'ru'), show_alert=True)


@mode_router.callback_query(F.data == "marathon_reset_do")
async def marathon_reset_do(callback: CallbackQuery):
    """Выполнить сброс марафона"""
    from db.queries.users import moscow_today

    chat_id = callback.message.chat.id
    today = moscow_today()
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    # Сбрасываем прогресс марафона
    await update_intern(chat_id,
        completed_topics=[],
        current_topic_index=0,
        marathon_start_date=today,
        marathon_status=MarathonStatus.ACTIVE,
        topics_today=0,
        topics_at_current_bloom=0,
    )

    await callback.answer(t('modes.marathon_reset', lang))

    await callback.message.edit_text(
        f"✅ *{t('modes.marathon_reset', lang)}*\n\n"
        f"{t('modes.new_start_date', lang)}: {today.strftime('%d.%m.%Y')}\n\n"
        f"{t('modes.use_learn_start', lang)}",
        parse_mode="Markdown"
    )


@mode_router.callback_query(F.data == "marathon_settings_back")
async def marathon_settings_back(callback: CallbackQuery):
    """Назад к настройкам марафона"""
    intern = await get_intern(callback.message.chat.id)
    await show_marathon_activated(callback.message, intern, feed_paused=False, edit=True)
    await callback.answer()


@mode_router.callback_query(F.data == "marathon_go_update")
async def marathon_go_update(callback: CallbackQuery, state: FSMContext):
    """Переход к обновлению профиля"""
    from bot import kb_update_profile, get_marathon_day, STUDY_DURATIONS, BLOOM_LEVELS, UpdateStates
    from i18n import get_language_name

    await callback.answer()

    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru')

    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    # Получаем дату старта марафона
    start_date = intern.get('marathon_start_date')
    if start_date:
        from datetime import datetime
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        marathon_start_str = start_date.strftime('%d.%m.%Y')
    else:
        marathon_start_str = "—"

    marathon_day = get_marathon_day(intern)

    interests_str = ', '.join(intern['interests']) if intern['interests'] else '—'
    motivation_short = intern.get('motivation', '')[:80] + '...' if len(intern.get('motivation', '')) > 80 else intern.get('motivation', '') or '—'
    goals_short = intern['goals'][:80] + '...' if len(intern['goals']) > 80 else intern['goals'] or '—'

    text = (
        f"👤 *{intern['name']}*\n"
        f"💼 {intern.get('occupation', '') or '—'}\n"
        f"🎨 {interests_str}\n\n"
        f"💫 {motivation_short}\n"
        f"🎯 {goals_short}\n\n"
        f"{duration.get('emoji', '')} {duration.get('name', '')}\n"
        f"{bloom['emoji']} {bloom['short_name']}\n"
        f"🗓 {marathon_start_str} ({t('progress.day', lang, n=marathon_day)})\n"
        f"⏰ {intern['schedule_time']}\n"
        f"🌐 {get_language_name(lang)}\n\n"
        f"*{t('settings.what_to_change', lang)}*"
    )

    # Редактируем текущее сообщение вместо удаления
    await callback.message.edit_text(text, reply_markup=kb_update_profile(lang), parse_mode="Markdown")
    await state.set_state(UpdateStates.choosing_field)


@mode_router.callback_query(F.data == "marathon_reminders_input")
async def marathon_reminders_input(callback: CallbackQuery, state: FSMContext):
    """Запрос ввода времени напоминаний"""
    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    schedule_time = intern.get('schedule_time', '09:00')
    schedule_time_2 = intern.get('schedule_time_2')

    text = f"⏰ *{t('modes.reminders_title', lang)}*\n\n"
    text += f"{t('modes.current_time', lang)}: {schedule_time}"
    if schedule_time_2:
        text += f", {schedule_time_2}"
    text += "\n\n"
    text += f"{t('modes.enter_time_format', lang)}\n"
    text += f"{t('modes.time_example', lang)}\n\n"
    text += f"_{t('modes.two_reminders_hint', lang)}_\n"
    text += f"_{t('modes.two_reminders_example', lang)}_"

    # Устанавливаем FSM-состояние ожидания ввода времени
    await state.set_state(MarathonSettingsStates.waiting_for_time)

    buttons = [[InlineKeyboardButton(text=t('buttons.back', lang), callback_data="marathon_cancel_input")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()


@mode_router.callback_query(F.data == "marathon_cancel_input")
async def marathon_cancel_input(callback: CallbackQuery, state: FSMContext):
    """Отмена ввода времени"""
    chat_id = callback.message.chat.id
    await state.clear()

    intern = await get_intern(chat_id)
    await show_marathon_activated(callback.message, intern, feed_paused=False, edit=True)
    await callback.answer()


@mode_router.message(MarathonSettingsStates.waiting_for_time)
async def on_marathon_time_input(message: Message, state: FSMContext):
    """Обработка ввода времени напоминаний"""
    chat_id = message.chat.id
    text = message.text.strip()

    # Регулярное выражение для времени ЧЧ:ММ
    time_pattern = r'^\d{1,2}:\d{2}$'

    # Разбираем ввод (одно или два времени через запятую)
    times = [time_str.strip() for time_str in text.split(',')]

    valid_times = []
    for time_str in times[:2]:  # Максимум 2 напоминания
        if re.match(time_pattern, time_str):
            # Проверяем корректность времени
            try:
                hours, minutes = map(int, time_str.split(':'))
                if 0 <= hours <= 23 and 0 <= minutes <= 59:
                    # Форматируем с ведущим нулём
                    valid_times.append(f"{hours:02d}:{minutes:02d}")
            except ValueError:
                pass

    if not valid_times:
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
        await message.answer(
            f"❌ {t('modes.invalid_time_format', lang)}\n\n"
            f"{t('modes.enter_time_example', lang)}",
            parse_mode="Markdown"
        )
        return

    # Сохраняем времена
    schedule_time = valid_times[0]
    schedule_time_2 = valid_times[1] if len(valid_times) > 1 else None

    await update_intern(chat_id, schedule_time=schedule_time, schedule_time_2=schedule_time_2)

    # Получаем данные состояния для определения куда возвращаться
    state_data = await state.get_data()
    return_to = state_data.get('return_to', 'marathon')

    await state.clear()

    # Показываем подтверждение
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    if schedule_time_2:
        confirm_text = f"✅ {t('modes.reminders_set', lang, time1=schedule_time, time2=schedule_time_2)}"
    else:
        confirm_text = f"✅ {t('modes.reminder_set', lang, time=schedule_time)}"

    await message.answer(confirm_text)

    # Возвращаемся к нужному экрану
    intern = await get_intern(chat_id)
    if return_to == 'feed':
        # Показываем экран Ленты
        await show_feed_activated(message, intern)
    else:
        # Показываем экран Марафона
        await show_marathon_activated(message, intern, feed_paused=False, edit=False)


# ==================== НАСТРОЙКА НАПОМИНАНИЙ ====================

@mode_router.callback_query(F.data == "marathon_set_reminders")
async def marathon_set_reminders(callback: CallbackQuery):
    """Меню настройки напоминаний"""
    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    schedule_time = intern.get('schedule_time', '09:00')
    schedule_time_2 = intern.get('schedule_time_2')

    text = f"⏰ *{t('modes.reminders_title', lang)}*\n\n"
    text += f"{t('modes.current_time', lang)}: {schedule_time}"
    if schedule_time_2:
        text += f", {schedule_time_2}"
    text += "\n"

    buttons = []

    # Изменить первое время
    buttons.append([InlineKeyboardButton(
        text=f"🕐 {t('modes.change_time', lang)} ({schedule_time})",
        callback_data="marathon_reminder_1"
    )])

    # Второе напоминание
    if schedule_time_2:
        buttons.append([InlineKeyboardButton(
            text=f"🕐 {t('modes.second', lang)}: {schedule_time_2} ❌",
            callback_data="marathon_reminder_2_remove"
        )])
    else:
        buttons.append([InlineKeyboardButton(
            text=f"➕ {t('modes.add_second', lang)}",
            callback_data="marathon_reminder_2_add"
        )])

    buttons.append([InlineKeyboardButton(
        text=t('buttons.back', lang),
        callback_data="marathon_settings_back"
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()


@mode_router.callback_query(F.data == "marathon_reminder_1")
async def marathon_reminder_1(callback: CallbackQuery):
    """Выбор первого времени напоминания"""
    await show_time_picker(callback, "reminder_1")


@mode_router.callback_query(F.data == "marathon_reminder_2_add")
async def marathon_reminder_2_add(callback: CallbackQuery):
    """Добавить второе напоминание"""
    await show_time_picker(callback, "reminder_2")


@mode_router.callback_query(F.data == "marathon_reminder_2_remove")
async def marathon_reminder_2_remove(callback: CallbackQuery):
    """Удалить второе напоминание"""
    await update_intern(callback.message.chat.id, schedule_time_2=None)
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    await callback.answer(t('modes.second_reminder_removed', lang))

    # Возвращаемся к настройкам напоминаний
    await marathon_set_reminders(callback)


async def show_time_picker(callback: CallbackQuery, target: str):
    """Показать выбор времени"""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'

    times = ["06:00", "07:00", "08:00", "09:00", "10:00", "11:00",
             "12:00", "18:00", "19:00", "20:00", "21:00", "22:00"]

    buttons = []
    row = []
    for i, time in enumerate(times):
        row.append(InlineKeyboardButton(
            text=time,
            callback_data=f"marathon_time_{target}_{time}"
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(
        text=t('buttons.back', lang),
        callback_data="marathon_set_reminders"
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
        f"⏰ {t('modes.select_time', lang)}:",
        reply_markup=keyboard
    )
    await callback.answer()


@mode_router.callback_query(F.data.startswith("marathon_time_"))
async def marathon_time_selected(callback: CallbackQuery):
    """Обработка выбора времени"""
    parts = callback.data.split("_")
    # marathon_time_reminder_1_09:00 или marathon_time_reminder_2_21:00
    target = parts[2] + "_" + parts[3]  # reminder_1 или reminder_2
    time = parts[4]

    if target == "reminder_1":
        await update_intern(callback.message.chat.id, schedule_time=time)
    else:
        await update_intern(callback.message.chat.id, schedule_time_2=time)

    # Возвращаемся к настройкам марафона
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'
    await callback.answer(t('modes.time_set', lang, time=time))
    await show_marathon_settings(callback.message, intern, edit=True)


# ==================== НАСТРОЙКА СЛОЖНОСТИ ====================

@mode_router.callback_query(F.data == "marathon_set_difficulty")
async def marathon_set_difficulty(callback: CallbackQuery):
    """Меню настройки сложности"""
    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    bloom_level = intern.get('bloom_level', 1)

    text = f"🎯 *{t('modes.complexity_title', lang)}*\n\n"

    levels = [
        (1, t('modes.level_basic', lang), t('modes.level_basic_desc', lang)),
        (2, t('modes.level_medium', lang), t('modes.level_medium_desc', lang)),
        (3, t('modes.level_advanced', lang), t('modes.level_advanced_desc', lang)),
    ]

    current_name = ""
    for lvl, name, desc in levels:
        mark = " ✓" if lvl == bloom_level else ""
        text += f"*{lvl}. {name}*{mark} — {desc}\n"
        if lvl == bloom_level:
            current_name = name

    text += f"\n{t('modes.current', lang)}: *{current_name}*"

    buttons = [
        [InlineKeyboardButton(text=f"1️⃣ {t('modes.level_basic', lang)}", callback_data="marathon_diff_1")],
        [InlineKeyboardButton(text=f"2️⃣ {t('modes.level_medium', lang)}", callback_data="marathon_diff_2")],
        [InlineKeyboardButton(text=f"3️⃣ {t('modes.level_advanced', lang)}", callback_data="marathon_diff_3")],
        [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="marathon_settings_back")]
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()


@mode_router.callback_query(F.data.startswith("marathon_diff_"))
async def marathon_difficulty_selected(callback: CallbackQuery):
    """Обработка выбора сложности"""
    level = int(callback.data.split("_")[2])

    await update_intern(callback.message.chat.id, bloom_level=level)

    # Возвращаемся к настройкам марафона
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'
    names = {1: t('modes.level_basic', lang), 2: t('modes.level_medium', lang), 3: t('modes.level_advanced', lang)}
    await callback.answer(t('modes.complexity_set', lang, level=names.get(level)))
    await show_marathon_settings(callback.message, intern, edit=True)


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
        settings_text = get_user_settings_text(intern, lang)

        # Проверяем, есть ли активная неделя
        from .feed.engine import FeedEngine
        engine = FeedEngine(chat_id)
        status = await engine.get_status()
        has_active_week = status.get('has_week') and status.get('week_status') == 'active'

        # Формируем текст сообщения
        text = f"{t('modes.feed_activated', lang)}\n\n"
        text += f"{t('modes.your_settings', lang)}\n{settings_text}\n"

        if has_active_week:
            # Есть активная неделя — показываем темы
            topics = status.get('topics', [])
            if topics:
                text += f"\n{t('feed.your_topics_label', lang)}\n"
                for i, topic in enumerate(topics, 1):
                    text += f"{i}. {topic}\n"

        # Если был активный марафон - ставим на паузу
        if (marathon_status == MarathonStatus.ACTIVE or
            (marathon_status == MarathonStatus.NOT_STARTED and has_marathon_progress)):
            await update_intern(chat_id,
                mode=Mode.FEED,
                marathon_status=MarathonStatus.PAUSED,
                feed_status=FeedStatus.ACTIVE,
            )
            text += f"\n\n{t('modes.marathon_paused', lang)}"
        else:
            await update_intern(chat_id,
                mode=Mode.FEED,
                feed_status=FeedStatus.ACTIVE,
            )

        # Кнопки в зависимости от наличия активной недели
        buttons = []

        if has_active_week:
            buttons.append([InlineKeyboardButton(
                text=f"📖 {t('buttons.get_digest', lang)}",
                callback_data="feed_get_digest"
            )])
            buttons.append([InlineKeyboardButton(
                text=f"📋 {t('buttons.topics_menu', lang)}",
                callback_data="feed_topics_menu"
            )])
        else:
            # Нет активной недели — кнопка для выбора тем
            buttons.append([InlineKeyboardButton(
                text=f"📚 {t('buttons.select_topics', lang)}",
                callback_data="feed_start_topics"
            )])

        # Общие кнопки настроек
        buttons.append([InlineKeyboardButton(text=t('buttons.update_profile', lang), callback_data="feed_go_update")])
        buttons.append([InlineKeyboardButton(text=t('buttons.reminders', lang), callback_data="feed_reminders_input")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в select_feed: {e}\n{traceback.format_exc()}")
        await callback.answer(t('errors.try_again', 'ru'), show_alert=True)


# ==================== КНОПКИ ЛЕНТЫ ====================

@mode_router.callback_query(F.data == "feed_go_update")
async def feed_go_update(callback: CallbackQuery, state: FSMContext):
    """Переход к обновлению профиля из Ленты"""
    from bot import kb_update_profile, get_marathon_day, STUDY_DURATIONS, BLOOM_LEVELS, UpdateStates
    from i18n import get_language_name

    await callback.answer()

    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru')

    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    bloom = BLOOM_LEVELS.get(intern['bloom_level'], BLOOM_LEVELS[1])

    # Получаем дату старта марафона
    start_date = intern.get('marathon_start_date')
    if start_date:
        from datetime import datetime
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        marathon_start_str = start_date.strftime('%d.%m.%Y')
    else:
        marathon_start_str = "—"

    marathon_day = get_marathon_day(intern)

    interests_str = ', '.join(intern['interests']) if intern['interests'] else '—'
    motivation_short = intern.get('motivation', '')[:80] + '...' if len(intern.get('motivation', '')) > 80 else intern.get('motivation', '') or '—'
    goals_short = intern['goals'][:80] + '...' if len(intern['goals']) > 80 else intern['goals'] or '—'

    text = (
        f"👤 *{intern['name']}*\n"
        f"💼 {intern.get('occupation', '') or '—'}\n"
        f"🎨 {interests_str}\n\n"
        f"💫 {motivation_short}\n"
        f"🎯 {goals_short}\n\n"
        f"{duration.get('emoji', '')} {duration.get('name', '')}\n"
        f"{bloom['emoji']} {bloom['short_name']}\n"
        f"🗓 {marathon_start_str} ({t('progress.day', lang, n=marathon_day)})\n"
        f"⏰ {intern['schedule_time']}\n"
        f"🌐 {get_language_name(lang)}\n\n"
        f"*{t('settings.what_to_change', lang)}*"
    )

    # Редактируем текущее сообщение вместо удаления
    await callback.message.edit_text(text, reply_markup=kb_update_profile(lang), parse_mode="Markdown")
    await state.set_state(UpdateStates.choosing_field)


@mode_router.callback_query(F.data == "feed_reminders_input")
async def feed_reminders_input(callback: CallbackQuery, state: FSMContext):
    """Запрос ввода времени напоминаний для Ленты"""
    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    schedule_time = intern.get('schedule_time', '09:00')
    schedule_time_2 = intern.get('schedule_time_2')

    text = f"⏰ *{t('modes.reminders_title', lang)}*\n\n"
    text += f"{t('modes.current_time', lang)}: {schedule_time}"
    if schedule_time_2:
        text += f", {schedule_time_2}"
    text += "\n\n"
    text += f"{t('modes.enter_time_format', lang)}\n"
    text += f"{t('modes.time_example', lang)}\n\n"
    text += f"_{t('modes.two_reminders_hint', lang)}_\n"
    text += f"_{t('modes.two_reminders_example', lang)}_"

    # Устанавливаем FSM-состояние (используем то же что и для марафона)
    await state.set_state(MarathonSettingsStates.waiting_for_time)
    # Сохраняем что это для ленты
    await state.update_data(return_to='feed')

    buttons = [[InlineKeyboardButton(text=t('buttons.back', lang), callback_data="feed_cancel_input")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()


@mode_router.callback_query(F.data == "feed_cancel_input")
async def feed_cancel_input(callback: CallbackQuery, state: FSMContext):
    """Отмена ввода времени для Ленты"""
    await state.clear()
    # Возвращаемся к выбору режима — вызываем select_feed
    await select_feed(callback)


def get_mode_name(mode: str, lang: str = 'ru') -> str:
    """Возвращает название режима"""
    mode_keys = {
        Mode.MARATHON: 'modes.marathon_name',
        Mode.FEED: 'modes.feed_name',
        Mode.BOTH: 'modes.both_name',
    }
    key = mode_keys.get(mode)
    return t(key, lang) if key else t('modes.not_selected', lang)


def get_marathon_status_text(intern: dict, lang: str = 'ru') -> str:
    """Возвращает текст статуса марафона"""
    status = intern.get('marathon_status', MarathonStatus.NOT_STARTED)
    completed = intern.get('completed_topics', [])
    topic_index = intern.get('current_topic_index', 0)

    # Вычисляем день (каждый день = 2 темы)
    day = (topic_index // 2) + 1 if topic_index > 0 else 0

    # Для legacy пользователей: если есть прогресс, но статус not_started — считаем активным
    has_progress = len(completed) > 0 or topic_index > 0

    if status == MarathonStatus.COMPLETED or (has_progress and day > 14):
        return t('modes.marathon_completed', lang)
    elif status == MarathonStatus.ACTIVE or (status == MarathonStatus.NOT_STARTED and has_progress):
        return t('modes.marathon_active', lang, day=day, completed=len(completed))
    elif status == MarathonStatus.PAUSED:
        return t('modes.marathon_on_pause', lang, day=day)
    elif status == MarathonStatus.NOT_STARTED:
        return t('modes.marathon_not_started', lang)
    return t('modes.status_unknown', lang)


def get_feed_status_text(intern: dict, lang: str = 'ru') -> str:
    """Возвращает текст статуса ленты"""
    status = intern.get('feed_status', FeedStatus.NOT_STARTED)
    active_days = intern.get('active_days_total', 0)

    if status == FeedStatus.NOT_STARTED:
        return t('modes.feed_not_started', lang)
    elif status == FeedStatus.ACTIVE:
        return t('modes.feed_active', lang, days=active_days)
    elif status == FeedStatus.PAUSED:
        return t('modes.feed_on_pause', lang, days=active_days)
    return t('modes.status_unknown', lang)


def get_complexity_name(level: int, lang: str = 'ru') -> str:
    """Возвращает название уровня сложности"""
    key = f'modes.complexity_{level}'
    # Пробуем получить перевод для конкретного уровня
    result = t(key, lang)
    if result == key or not result:
        # Если ключ не найден, используем общий формат
        return t('modes.complexity_level', lang, level=level)
    return result


def get_user_settings_text(intern: dict, lang: str = 'ru') -> str:
    """Формирует текст с настройками пользователя"""
    schedule_time = intern.get('schedule_time', '09:00')
    schedule_time_2 = intern.get('schedule_time_2')
    study_duration = intern.get('study_duration', 15)
    complexity = intern.get('complexity_level') or intern.get('bloom_level', 1)

    text = f"{t('modes.time_label', lang)} {schedule_time}\n"
    text += f"{t('modes.duration_label', lang)} {study_duration} {t('modes.min_suffix', lang)}\n"
    text += f"{t('modes.complexity_label', lang)} {get_complexity_name(complexity, lang)}"

    if schedule_time_2:
        text += f"\n{t('modes.extra_reminder', lang)} {schedule_time_2}"

    return text
