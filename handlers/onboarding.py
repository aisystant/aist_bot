"""
Хендлеры онбординга — /start и все шаги регистрации.

Использует legacy aiogram FSM (OnboardingStates).
Вся логика собрана здесь, bot.py только импортирует OnboardingStates.
"""

import logging
from datetime import timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import STUDY_DURATIONS, MARATHON_DAYS
from db.queries import get_intern, update_intern
from db.queries.users import moscow_today, get_slot_load, MAX_USERS_PER_SLOT
from i18n import t, detect_language, get_language_name, SUPPORTED_LANGUAGES
from integrations.telegram.keyboards import (
    kb_study_duration, kb_marathon_start, kb_confirm, kb_learn, kb_language_select,
    kb_slot_suggestions,
)

logger = logging.getLogger(__name__)

onboarding_router = Router(name="onboarding")


# ============= СОСТОЯНИЯ FSM =============

class OnboardingStates(StatesGroup):
    """Онбординг для марафона"""
    choosing_language = State()          # 0. Язык (для неподдерживаемых языков)
    waiting_for_name = State()           # 1. Имя
    waiting_for_occupation = State()     # 2. Чем занимаешься
    waiting_for_interests = State()      # 3. Интересы/хобби
    waiting_for_motivation = State()     # 4. Что важно в жизни
    waiting_for_goals = State()          # 5. Что хочешь изменить
    waiting_for_study_duration = State() # 6. Время на тему
    waiting_for_schedule = State()       # 7. Время напоминания
    waiting_for_start_date = State()     # 8. Дата старта марафона
    confirming_profile = State()


# ============= ВСПОМОГАТЕЛЬНЫЕ =============

async def get_lang(state: FSMContext, intern: dict = None) -> str:
    """Получить язык из state или из профиля пользователя."""
    data = await state.get_data()
    if 'lang' in data:
        return data['lang']
    if intern and 'language' in intern:
        return intern['language']
    return 'ru'


# ============= ВСПОМОГАТЕЛЬНЫЕ (RESET) =============

def _has_learning_data(intern: dict) -> bool:
    """Есть ли у пользователя учебные данные (марафон или лента)."""
    completed = intern.get('completed_topics', [])
    if completed and len(completed) > 0:
        return True
    if intern.get('marathon_status') not in ('not_started', None):
        return True
    if intern.get('feed_status') not in ('not_started', None):
        return True
    return False


# ============= ХЕНДЛЕРЫ =============

@onboarding_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)

    if intern['onboarding_completed']:
        # Очищаем legacy FSM state
        await state.clear()

        # Проверяем наличие старых учебных данных → предлагаем сброс (одноразово)
        ctx = intern.get('current_context', {})
        reset_offered = ctx.get('reset_offered', False)
        if not reset_offered and _has_learning_data(intern):
            lang = intern.get('language', 'ru')
            completed_count = len(intern.get('completed_topics', []))
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('reset.fresh_start_btn', lang),
                    callback_data="reset_all_progress",
                )],
                [InlineKeyboardButton(
                    text=t('reset.continue_btn', lang),
                    callback_data="reset_skip",
                )],
            ])
            await message.answer(
                t('reset.old_data_detected', lang, completed=completed_count),
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return

        # Если SM активна — переводим в mode_select через Dispatcher
        from handlers import get_dispatcher
        dispatcher = get_dispatcher()
        if dispatcher and dispatcher.is_sm_active:
            await dispatcher.route_command('mode', intern)
            return

        lang = intern.get('language', 'ru')

        # Определяем текущий режим
        from config import Mode
        current_mode = intern.get('mode', Mode.MARATHON)
        mode_emoji = "🏃" if current_mode == Mode.MARATHON else "📚"
        mode_name = t('help.marathon', lang) if current_mode == Mode.MARATHON else t('help.feed', lang)

        # Прогресс активности
        from db.queries.activity import get_activity_stats
        from core.topics import get_marathon_day
        stats = await get_activity_stats(message.chat.id)
        total_active = stats.get('total', 0)
        marathon_day = get_marathon_day(intern)

        # Send welcome with tier-based ReplyKeyboard (WP-52)
        from core.tier_ui import build_reply_keyboard, sync_menu_commands
        from core.tier_detector import detect_ui_tier
        tier = detect_ui_tier(intern)
        keyboard = build_reply_keyboard(tier, lang)

        await message.answer(
            t('welcome.returning', lang, name=intern['name']) + "\n" +
            f"{mode_emoji} {t('welcome.current_mode', lang)}: *{mode_name}*\n" +
            f"📊 {t('welcome.activity_progress', lang)}: {total_active} {t('shared.of', lang)} {marathon_day}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

        # Sync per-user menu commands
        await sync_menu_commands(message.bot, message.chat.id, tier, lang)
        return

    # Определяем язык интерфейса пользователя
    lang = detect_language(message.from_user.language_code)

    if lang in SUPPORTED_LANGUAGES:
        welcome_text = (
            t('welcome.greeting', lang) + "\n" +
            t('welcome.intro', lang) + "\n\n" +
            t('welcome.ask_name', lang)
        )
    else:
        # Для неизвестных языков — показываем выбор языка
        welcome_text = (
            t('welcome.greeting', 'en') + "\n" +
            t('welcome.intro', 'en') + "\n\n" +
            "🌐 *Choose your language:*"
        )
        await message.answer(welcome_text, reply_markup=kb_language_select(), parse_mode="Markdown")
        await state.set_state(OnboardingStates.choosing_language)
        return

    # Сохраняем определённый язык для дальнейшего использования
    await state.update_data(lang=lang)

    await message.answer(welcome_text)
    await state.set_state(OnboardingStates.waiting_for_name)


@onboarding_router.callback_query(OnboardingStates.choosing_language, F.data.startswith("lang_"))
async def on_choose_language(callback: CallbackQuery, state: FSMContext):
    """Выбор языка при онбординге (для неподдерживаемых языков Telegram)."""
    lang_code = callback.data.replace("lang_", "")
    if lang_code not in SUPPORTED_LANGUAGES:
        lang_code = 'en'

    await state.update_data(lang=lang_code)
    await callback.answer(t('settings.language.changed', lang_code))

    # Продолжаем онбординг — спрашиваем имя
    await callback.message.edit_text(
        t('welcome.greeting', lang_code) + "\n" +
        t('welcome.intro', lang_code) + "\n\n" +
        t('welcome.ask_name', lang_code)
    )
    await state.set_state(OnboardingStates.waiting_for_name)


@onboarding_router.message(OnboardingStates.waiting_for_name)
async def on_name(message: Message, state: FSMContext):
    lang = await get_lang(state)
    name = message.text.strip()
    await update_intern(message.chat.id, name=name, language=lang)
    await message.answer(
        t('onboarding.nice_to_meet', lang, name=name) + "\n\n" +
        t('onboarding.ask_occupation', lang) + "\n\n" +
        t('onboarding.ask_occupation_hint', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_occupation)

@onboarding_router.message(OnboardingStates.waiting_for_occupation)
async def on_occupation(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await update_intern(message.chat.id, occupation=message.text.strip())
    await message.answer(
        t('onboarding.ask_interests', lang) + "\n\n" +
        t('onboarding.ask_interests_hint', lang) + "\n\n" +
        t('onboarding.ask_interests_why', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_interests)

@onboarding_router.message(OnboardingStates.waiting_for_interests)
async def on_interests(message: Message, state: FSMContext):
    lang = await get_lang(state)
    interests = [i.strip() for i in message.text.replace(',', ';').split(';') if i.strip()]
    await update_intern(message.chat.id, interests=interests)
    await message.answer(
        f"*{t('onboarding.ask_values', lang)}*\n\n" +
        t('onboarding.ask_values_hint', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_motivation)

@onboarding_router.message(OnboardingStates.waiting_for_motivation)
async def on_motivation(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await update_intern(message.chat.id, motivation=message.text.strip())
    await message.answer(
        f"*{t('onboarding.ask_goals', lang)}*\n\n" +
        t('onboarding.ask_goals_hint', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_goals)

@onboarding_router.message(OnboardingStates.waiting_for_goals)
async def on_goals(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await update_intern(message.chat.id, goals=message.text.strip())
    await message.answer(
        t('onboarding.ask_duration', lang) + "\n\n",
        parse_mode="Markdown",
        reply_markup=kb_study_duration(lang)
    )
    await state.set_state(OnboardingStates.waiting_for_study_duration)

@onboarding_router.callback_query(OnboardingStates.waiting_for_study_duration, F.data.startswith("duration_"))
async def on_duration(callback: CallbackQuery, state: FSMContext):
    lang = await get_lang(state)
    duration = int(callback.data.replace("duration_", ""))
    await update_intern(callback.message.chat.id, study_duration=duration)
    await callback.answer()
    await callback.message.edit_text(
        t('onboarding.ask_time', lang) + "\n\n" +
        t('onboarding.ask_time_hint', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_schedule)

@onboarding_router.message(OnboardingStates.waiting_for_schedule)
async def on_schedule(message: Message, state: FSMContext):
    lang = await get_lang(state)
    try:
        h, m = map(int, message.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except:
        await message.answer(t('errors.try_again', lang) + " (ЧЧ:ММ)")
        return

    # Нормализуем формат времени (с ведущими нулями)
    normalized_time = f"{h:02d}:{m:02d}"

    # Проверяем загрузку слота
    counts = await get_slot_load(normalized_time)
    target_count = counts.get(normalized_time, 0)

    if target_count >= MAX_USERS_PER_SLOT:
        # Слот перегружен — показываем варианты
        await message.answer(
            f"⏰ {t('update.schedule_shifted', lang, requested=normalized_time, count=target_count)}:",
            reply_markup=kb_slot_suggestions(normalized_time, counts, lang)
        )
        return  # Остаёмся в waiting_for_schedule, ждём callback

    # Слот свободен — сохраняем и идём дальше
    await update_intern(message.chat.id, schedule_time=normalized_time)
    await message.answer(
        f"🗓 *{t('onboarding.ask_start_date', lang)}*\n\n" +
        t('modes.marathon_desc', lang),
        parse_mode="Markdown",
        reply_markup=kb_marathon_start(lang)
    )
    await state.set_state(OnboardingStates.waiting_for_start_date)


@onboarding_router.callback_query(OnboardingStates.waiting_for_schedule, F.data.startswith("slot_"))
async def on_slot_selected(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал слот из предложенных вариантов."""
    lang = await get_lang(state)
    selected_time = callback.data.replace("slot_", "")
    await callback.answer()

    await update_intern(callback.message.chat.id, schedule_time=selected_time)
    await callback.message.edit_text(
        f"✅ {t('update.schedule_changed', lang)}: *{selected_time}*\n\n"
        f"🗓 *{t('onboarding.ask_start_date', lang)}*\n\n" +
        t('modes.marathon_desc', lang),
        parse_mode="Markdown",
        reply_markup=kb_marathon_start(lang)
    )
    await state.set_state(OnboardingStates.waiting_for_start_date)


@onboarding_router.callback_query(OnboardingStates.waiting_for_start_date, F.data.startswith("start_"))
async def on_start_date(callback: CallbackQuery, state: FSMContext):
    today = moscow_today()

    if callback.data == "start_today":
        start_date = today
    elif callback.data == "start_tomorrow":
        start_date = today + timedelta(days=1)
    else:  # start_day_after
        start_date = today + timedelta(days=2)

    await update_intern(callback.message.chat.id, marathon_start_date=start_date)
    await callback.answer()

    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'

    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    interests_str = ', '.join(intern['interests']) if intern['interests'] else t('profile.not_specified_plural', lang)
    motivation_short = intern['motivation'][:100] + '...' if len(intern['motivation']) > 100 else intern['motivation']
    goals_short = intern['goals'][:100] + '...' if len(intern['goals']) > 100 else intern['goals']

    await callback.message.edit_text(
        f"📋 *{t('profile.your_profile', lang)}:*\n\n"
        f"👤 *{t('profile.name_label', lang)}:* {intern['name']}\n"
        f"💼 *{t('profile.occupation_label', lang)}:* {intern['occupation']}\n"
        f"🎨 *{t('profile.interests_label', lang)}:* {interests_str}\n\n"
        f"💫 *{t('profile.what_important', lang)}:* {motivation_short}\n"
        f"🎯 *{t('profile.what_change', lang)}:* {goals_short}\n\n"
        f"{duration.get('emoji', '')} {duration.get('name', '')} {t('profile.per_topic', lang)}\n"
        f"⏰ {t('profile.reminder_at', lang)} {intern['schedule_time']}\n"
        f"🗓 {t('profile.marathon_start', lang)}: *{start_date.strftime('%d.%m.%Y')}*\n\n"
        f"{t('profile.all_correct', lang)}",
        parse_mode="Markdown",
        reply_markup=kb_confirm(lang)
    )
    await state.set_state(OnboardingStates.confirming_profile)

@onboarding_router.callback_query(OnboardingStates.confirming_profile, F.data == "confirm")
async def on_confirm(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    try:
        from datetime import datetime
        await update_intern(
            chat_id,
            onboarding_completed=True,
            trial_started_at=datetime.utcnow(),  # naive UTC — DB column is TIMESTAMP (not TIMESTAMPTZ)
        )
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') or 'ru'

        from core.topics import get_marathon_day
        marathon_day = get_marathon_day(intern)
        start_date = intern.get('marathon_start_date')

        await callback.answer(t('update.saved', lang))

        # Определяем, когда старт
        if start_date:
            today = moscow_today()
            from datetime import datetime
            if isinstance(start_date, datetime):
                start_date = start_date.date()
            if start_date > today:
                start_msg = f"🗓 *{t('profile.marathon_will_start', lang, date=start_date.strftime('%d.%m.%Y'))}*"
            else:
                start_msg = f"🗓 *{t('progress.day', lang, day=marathon_day, total=MARATHON_DAYS)}*"
        else:
            start_msg = f"🗓 {t('profile.date_not_set', lang)}"

        # Приветственное сообщение для марафона
        await callback.message.edit_text(
            f"🎉 *{t('welcome.marathon_welcome', lang, name=intern['name'])}*\n\n"
            f"{t('welcome.marathon_intro', lang)}\n"
            f"📅 {t('welcome.marathon_days_info', lang, days=MARATHON_DAYS)}\n"
            f"⏱ {t('welcome.marathon_duration_info', lang, minutes=intern['study_duration'])}\n"
            f"⏰ {t('welcome.marathon_reminders_info', lang, time=intern['schedule_time'])}\n\n"
            f"{start_msg}",
            parse_mode="Markdown",
            reply_markup=kb_learn(lang)
        )

        # Send tier-based ReplyKeyboard + sync menu commands (WP-52)
        from core.tier_ui import send_tier_keyboard
        await send_tier_keyboard(callback.message, intern)

        await state.clear()
    except Exception as e:
        logger.error(f"[Onboarding] Error confirming profile for {chat_id}: {e}")
        lang = await get_lang(state)
        await callback.answer(t('errors.try_again', lang), show_alert=True)

@onboarding_router.callback_query(OnboardingStates.confirming_profile, F.data == "restart")
async def on_restart(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    try:
        await callback.answer()
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
        await callback.message.edit_text(t('onboarding.restart', lang))
        await state.set_state(OnboardingStates.waiting_for_name)
    except Exception as e:
        logger.error(f"[Onboarding] Error restarting profile for {chat_id}: {e}")
        await callback.answer(t('errors.try_again', 'ru'), show_alert=True)


# ============= СБРОС ПРОГРЕССА (авто-детект при /start) =============

@onboarding_router.callback_query(F.data == "reset_all_progress")
async def on_reset_all_progress(callback: CallbackQuery, state: FSMContext):
    """Полный сброс учебных данных с сохранением профиля."""
    chat_id = callback.from_user.id
    await callback.answer()

    try:
        from db.queries.profile import reset_learning_data
        result = await reset_learning_data(chat_id)
        total = sum(result.values())
        logger.info(f"[Reset] Full learning reset for {chat_id}: {total} rows affected")

        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru')

        await callback.message.edit_text(
            t('reset.done', lang),
            parse_mode="Markdown",
        )

        # Переводим в mode_select
        from handlers import get_dispatcher
        dispatcher = get_dispatcher()
        if dispatcher and dispatcher.is_sm_active:
            await dispatcher.route_command('mode', intern)
    except Exception as e:
        logger.error(f"[Reset] Error resetting {chat_id}: {e}")
        await callback.message.edit_text(t('errors.try_again', 'ru'))


@onboarding_router.callback_query(F.data == "reset_skip")
async def on_reset_skip(callback: CallbackQuery, state: FSMContext):
    """Пользователь решил продолжить с текущим прогрессом."""
    chat_id = callback.from_user.id
    await callback.answer()

    # Ставим флаг, чтобы не предлагать снова
    intern = await get_intern(chat_id)
    ctx = intern.get('current_context', {})
    ctx['reset_offered'] = True
    await update_intern(chat_id, current_context=ctx)

    try:
        await callback.message.delete()
    except Exception:
        pass

    intern = await get_intern(chat_id)

    from handlers import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        await dispatcher.route_command('mode', intern)
