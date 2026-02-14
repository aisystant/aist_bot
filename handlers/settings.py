"""
Хендлеры настроек и обновления профиля.

Включает:
- UpdateStates FSM
- _show_update_screen (legacy экран настроек)
- cmd_language, cmd_profile, cmd_help
- Все upd_* callback хендлеры и on_save_* хендлеры
"""

import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import STUDY_DURATIONS, MARATHON_DAYS
from db.queries import get_intern, update_intern
from db.queries.users import moscow_today
from i18n import t, get_language_name, SUPPORTED_LANGUAGES
from integrations.telegram.keyboards import (
    kb_update_profile, kb_study_duration, kb_bloom_level,
    kb_marathon_start, kb_language_select,
)

logger = logging.getLogger(__name__)

settings_router = Router(name="settings")


# ============= СОСТОЯНИЯ FSM =============

class UpdateStates(StatesGroup):
    choosing_field = State()
    updating_name = State()
    updating_occupation = State()
    updating_interests = State()
    updating_motivation = State()
    updating_goals = State()
    updating_duration = State()
    updating_schedule = State()
    updating_bloom_level = State()
    updating_marathon_start = State()


# ============= ВСПОМОГАТЕЛЬНЫЕ =============

async def _show_update_screen(message, intern, state):
    """Показать экран настроек."""
    from core.topics import get_marathon_day
    lang = intern.get('language', 'ru') or 'ru'
    study_duration = intern.get('study_duration') or 15
    bloom_level = intern.get('bloom_level') or 1
    bloom_emojis = {1: '🔵', 2: '🟡', 3: '🔴'}

    start_date = intern.get('marathon_start_date')
    if start_date:
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        marathon_start_str = start_date.strftime('%d.%m.%Y')
    else:
        marathon_start_str = "—"

    marathon_day = get_marathon_day(intern)
    interests_str = ', '.join(intern.get('interests', [])) if intern.get('interests') else '—'
    motivation_short = intern.get('motivation', '')[:80] + '...' if len(intern.get('motivation', '')) > 80 else intern.get('motivation', '') or '—'
    goals_short = (intern.get('goals') or '')[:80] + '...' if len(intern.get('goals') or '') > 80 else intern.get('goals') or '—'

    await message.answer(
        f"👤 *{intern.get('name', '—')}*\n"
        f"💼 {intern.get('occupation', '') or '—'}\n"
        f"🎨 {interests_str}\n\n"
        f"💫 {motivation_short}\n"
        f"🎯 {goals_short}\n\n"
        f"{t(f'duration.minutes_{study_duration}', lang)}\n"
        f"{bloom_emojis.get(bloom_level, '🔵')} {t(f'bloom.level_{bloom_level}_short', lang)}\n"
        f"🗓 {marathon_start_str} ({t('progress.day', lang, day=marathon_day, total=14)})\n"
        f"⏰ {intern.get('schedule_time', '09:00')} (МСК)\n"
        f"🌐 {get_language_name(lang)}\n\n"
        f"*{t('settings.what_to_change', lang)}*",
        parse_mode="Markdown",
        reply_markup=kb_update_profile(lang)
    )
    await state.set_state(UpdateStates.choosing_field)


# ============= КОМАНДЫ =============

@settings_router.message(Command("profile"))
async def cmd_profile(message: Message):
    from core.topics import get_marathon_day
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')

    if not intern['onboarding_completed']:
        await message.answer(t('profile.first_start', lang))
        return

    study_duration = intern['study_duration']
    bloom_level = intern['bloom_level']
    bloom_emojis = {1: '🔵', 2: '🟡', 3: '🔴'}

    interests_str = ', '.join(intern['interests']) if intern['interests'] else t('profile.not_specified', lang)
    motivation_short = intern['motivation'][:100] + '...' if len(intern.get('motivation', '')) > 100 else intern.get('motivation', '')
    goals_short = intern['goals'][:100] + '...' if len(intern['goals']) > 100 else intern['goals']

    marathon_day = get_marathon_day(intern)
    start_date = intern.get('marathon_start_date')
    marathon_start_str = start_date.strftime('%d.%m.%Y') if start_date else t('profile.date_not_set', lang)

    # Оценка систематичности
    assessment_state = intern.get('assessment_state')
    assessment_date = intern.get('assessment_date')
    if assessment_state and assessment_date:
        from core.assessment import load_assessment
        assess_data = load_assessment('systematicity')
        group = None
        if assess_data:
            group = next(
                (g for g in assess_data.get('groups', []) if g['id'] == assessment_state),
                None,
            )
        if group:
            a_emoji = group.get('emoji', '')
            a_title = group.get('title', {}).get(lang, group.get('title', {}).get('ru', assessment_state))
            a_date = assessment_date.strftime('%d.%m.%Y') if hasattr(assessment_date, 'strftime') else str(assessment_date)
            assessment_line = f"📋 {t('assessment.profile_label', lang)}: {a_emoji} {a_title} ({a_date})"
        else:
            assessment_line = f"📋 {t('assessment.profile_label', lang)}: {assessment_state}"
    else:
        assessment_line = f"📋 {t('assessment.profile_label', lang)}: {t('assessment.profile_not_tested', lang)}"

    await message.answer(
        f"👤 *{intern['name']}*\n"
        f"💼 {intern.get('occupation', '')}\n"
        f"🎨 {interests_str}\n\n"
        f"💫 *{t('profile.what_important', lang)}:* {motivation_short or t('profile.not_specified', lang)}\n"
        f"🎯 *{t('profile.what_change', lang)}:* {goals_short or t('profile.not_specified', lang)}\n\n"
        f"{STUDY_DURATIONS.get(str(study_duration), {}).get('emoji', '')} "
        f"{STUDY_DURATIONS.get(str(study_duration), {}).get('name', '')} {t('profile.per_topic', lang)}\n"
        f"{bloom_emojis.get(bloom_level, '🔵')} {t(f'bloom.level_{bloom_level}_short', lang)}\n"
        f"🗓 {marathon_start_str} ({t('progress.day', lang, day=marathon_day, total=MARATHON_DAYS)})\n"
        f"⏰ {intern.get('schedule_time', '09:00')} (МСК)\n"
        f"{assessment_line}\n\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )


@settings_router.message(Command("help"))
async def cmd_help(message: Message):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    text = (
        f"*{t('help.title', lang)}*\n\n"
        f"{t('help.intro', lang)}\n\n"
        f"{t('help.marathon_short', lang)}\n"
        f"{t('help.feed_short', lang)}\n\n"
        f"{t('help.ai_hint', lang)}\n"
        f"{t('help.notes_hint', lang)}\n\n"
        f"{t('help.schedule_hint', lang)}\n\n"
        f"*{t('help.feedback', lang)}:* /feedback {t('help.feedback_or', lang)} !текст"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 " + t('help.all_commands', lang), callback_data="help_all_commands")]
    ])
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


@settings_router.callback_query(F.data == "help_all_commands")
async def cb_help_all_commands(callback: CallbackQuery):
    """Показать все команды бота."""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    await callback.answer()

    text = (
        f"*{t('help.commands_title', lang)}*\n\n"
        f"*{t('commands.section_main', lang)}*\n"
        f"{t('commands.start', lang)}\n"
        f"{t('commands.mode', lang)}\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.feed', lang)}\n"
        f"{t('commands.progress', lang)}\n"
        f"{t('commands.test', lang)}\n"
        f"{t('commands.plan', lang)}\n"
        f"{t('commands.mydata', lang)}\n\n"
        f"*{t('commands.section_settings', lang)}*\n"
        f"{t('commands.profile', lang)}\n"
        f"{t('commands.settings', lang)}\n"
        f"{t('commands.help', lang)}\n"
        f"{t('commands.language', lang)}\n\n"
        f"*{t('commands.section_special', lang)}*\n"
        f"{t('commands.notes', lang)}\n"
        f"{t('commands.consultation', lang)}\n"
        f"{t('commands.github', lang)}\n"
        f"{t('commands.feedback', lang)}"
    )

    await callback.message.edit_text(text, parse_mode="Markdown")


@settings_router.message(Command("language"))
async def cmd_language(message: Message, state: FSMContext):
    """Команда смены языка напрямую."""
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    await message.answer(
        t('settings.language.title', lang),
        reply_markup=kb_language_select()
    )
    await state.set_state(UpdateStates.choosing_field)


# ============= UPDATE CALLBACKS =============

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_name")
async def on_upd_name(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        f"👤 *{t('update.your_name', lang)}:* {intern['name']}\n\n"
        f"{t('update.whats_your_name', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_name)

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_occupation")
async def on_upd_occupation(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        f"💼 *{t('update.your_occupation', lang)}:* {intern.get('occupation', '') or t('profile.not_specified', lang)}\n\n"
        f"{t('update.whats_your_occupation', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_occupation)

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_interests")
async def on_upd_interests(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    interests_str = ', '.join(intern['interests']) if intern['interests'] else t('profile.not_specified', lang)
    await callback.answer()
    await callback.message.edit_text(
        f"🎨 *{t('update.your_interests', lang)}:* {interests_str}\n\n"
        f"{t('update.what_interests', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_interests)

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_motivation")
async def on_upd_motivation(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    await callback.answer()
    await callback.message.edit_text(
        f"💫 *Что сейчас важно:*\n{intern.get('motivation', '') or 'не указано'}\n\n"
        "Что для вас по-настоящему важно в жизни?",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_motivation)

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_goals")
async def on_upd_goals(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        f"🎯 *{t('update.your_goals', lang)}:*\n{intern['goals'] or t('profile.not_specified', lang)}\n\n"
        f"{t('update.what_goals', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_goals)

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_duration")
async def on_upd_duration(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})
    await callback.answer()
    await callback.message.edit_text(
        f"⏱ *{t('update.current_time', lang)}:* {duration.get('emoji', '')} {duration.get('name', '')}\n\n"
        f"{t('update.how_many_minutes', lang)}",
        parse_mode="Markdown",
        reply_markup=kb_study_duration(lang)
    )
    await state.set_state(UpdateStates.updating_duration)

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_schedule")
async def on_upd_schedule(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        f"⏰ *{t('update.current_schedule', lang)}:* {intern['schedule_time']} (МСК)\n\n"
        f"{t('update.when_remind', lang)}",
        parse_mode="Markdown"
    )
    await state.set_state(UpdateStates.updating_schedule)

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_bloom")
async def on_upd_bloom(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    level = intern['bloom_level']
    emojis = {1: '🔵', 2: '🟡', 3: '🔴'}
    await callback.answer()
    await callback.message.edit_text(
        f"🎚 *{t('update.current_difficulty', lang)}:* {emojis.get(level, '🔵')} {t(f'bloom.level_{level}_short', lang)}\n"
        f"_{t(f'bloom.level_{level}_desc', lang)}_\n\n"
        f"📊 *{t('update.difficulty_scale', lang)}:* 1 — {t('update.easiest', lang)}, 3 — {t('update.hardest', lang)}\n\n"
        f"{t('update.select_difficulty', lang)}",
        parse_mode="Markdown",
        reply_markup=kb_bloom_level(lang)
    )
    await state.set_state(UpdateStates.updating_bloom_level)

@settings_router.callback_query(UpdateStates.updating_bloom_level, F.data.startswith("bloom_"))
async def on_save_bloom(callback: CallbackQuery, state: FSMContext):
    level = int(callback.data.replace("bloom_", ""))
    await update_intern(callback.message.chat.id, bloom_level=level, topics_at_current_bloom=0)

    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    await callback.answer(f"{t(f'bloom.level_{level}_short', lang)}")
    await callback.message.edit_text(
        f"✅ {t('update.difficulty_changed', lang)}: *{t(f'bloom.level_{level}_short', lang)}*!\n\n"
        f"{t(f'bloom.level_{level}_desc', lang)}\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.mode', lang)}\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_mode")
async def on_upd_mode(callback: CallbackQuery, state: FSMContext):
    """Переход к выбору режима (Марафон/Лента)."""
    await state.clear()
    await callback.answer()

    try:
        from engines.mode_selector import cmd_mode
        await cmd_mode(callback.message)
    except ImportError:
        await callback.message.edit_text(
            "🎯 *Выбор режима*\n\n"
            "Используйте команду /mode для выбора режима работы.",
            parse_mode="Markdown"
        )

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_marathon_start")
async def on_upd_marathon_start(callback: CallbackQuery, state: FSMContext):
    from core.topics import get_marathon_day
    intern = await get_intern(callback.message.chat.id)
    start_date = intern.get('marathon_start_date')
    marathon_day = get_marathon_day(intern)

    if start_date:
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        current_date_str = start_date.strftime('%d.%m.%Y')
    else:
        current_date_str = "не задана"

    await callback.answer()
    await callback.message.edit_text(
        f"🗓 *Текущая дата старта:* {current_date_str}\n"
        f"*День марафона:* {marathon_day} из {MARATHON_DAYS}\n\n"
        f"⚠️ *Внимание:* изменение даты старта влияет на расчёт текущего дня марафона.\n\n"
        f"Выберите новую дату старта:",
        parse_mode="Markdown",
        reply_markup=kb_marathon_start()
    )
    await state.set_state(UpdateStates.updating_marathon_start)

@settings_router.callback_query(UpdateStates.updating_marathon_start, F.data.startswith("start_"))
async def on_save_marathon_start(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    today = moscow_today()

    if callback.data == "start_today":
        start_date = today
        date_text = t('update.today', lang)
    elif callback.data == "start_tomorrow":
        start_date = today + timedelta(days=1)
        date_text = t('update.tomorrow', lang)
    else:  # start_day_after
        start_date = today + timedelta(days=2)
        date_text = t('update.day_after_tomorrow', lang)

    await update_intern(callback.message.chat.id, marathon_start_date=start_date)

    await callback.answer(t('update.start_date_updated', lang))
    await callback.message.edit_text(
        f"✅ {t('update.marathon_start_changed', lang)}\n\n"
        f"{t('update.new_date', lang)}: *{start_date.strftime('%d.%m.%Y')}* ({date_text})\n\n"
        f"{t('update.continue_learning_hint', lang)}\n"
        f"{t('update.update_more', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()

@settings_router.callback_query(UpdateStates.choosing_field, F.data == "upd_language")
async def on_upd_language(callback: CallbackQuery, state: FSMContext):
    """Показать меню выбора языка."""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    await callback.answer()
    await callback.message.edit_text(
        t('settings.language.title', lang),
        reply_markup=kb_language_select()
    )

@settings_router.callback_query(UpdateStates.choosing_field, F.data.startswith("lang_"))
async def on_select_language(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор языка."""
    new_lang = callback.data.replace("lang_", "")
    if new_lang not in SUPPORTED_LANGUAGES:
        new_lang = 'ru'

    await update_intern(callback.message.chat.id, language=new_lang)
    await callback.answer(t('settings.language.changed', new_lang))
    await callback.message.edit_text(
        t('settings.language.changed', new_lang) + "\n\n" +
        t('commands.learn', new_lang) + "\n" +
        t('commands.update', new_lang)
    )
    await state.clear()


# ============= SAVE HANDLERS =============

@settings_router.message(UpdateStates.updating_motivation)
async def on_save_motivation(message: Message, state: FSMContext):
    await update_intern(message.chat.id, motivation=message.text.strip())
    await message.answer(
        "✅ Обновлено!\n\n"
        "Теперь мотивационные блоки будут ещё точнее.\n\n"
        "/learn — продолжить обучение\n"
        "/update — изменить ещё что-то"
    )
    await state.clear()

@settings_router.message(UpdateStates.updating_goals)
async def on_save_goals(message: Message, state: FSMContext):
    await update_intern(message.chat.id, goals=message.text.strip())
    await message.answer(
        "✅ Обновлено!\n\n"
        "Теперь материалы будут персонализированы под ваши цели.\n\n"
        "/learn — продолжить обучение\n"
        "/update — изменить ещё что-то"
    )
    await state.clear()

@settings_router.message(UpdateStates.updating_name)
async def on_save_name(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')
    await update_intern(message.chat.id, name=message.text.strip())
    await message.answer(
        f"✅ {t('update.name_changed', lang)}: *{message.text.strip()}*\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()

@settings_router.message(UpdateStates.updating_occupation)
async def on_save_occupation(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')
    await update_intern(message.chat.id, occupation=message.text.strip())
    await message.answer(
        f"✅ {t('update.occupation_changed', lang)}!\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}"
    )
    await state.clear()

@settings_router.message(UpdateStates.updating_interests)
async def on_save_interests(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')
    interests = [i.strip() for i in message.text.replace(',', ';').split(';') if i.strip()]
    await update_intern(message.chat.id, interests=interests)
    await message.answer(
        f"✅ {t('update.interests_changed', lang)}!\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}"
    )
    await state.clear()

@settings_router.callback_query(UpdateStates.updating_duration, F.data.startswith("duration_"))
async def on_save_duration(callback: CallbackQuery, state: FSMContext):
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru')
    duration = int(callback.data.replace("duration_", ""))
    await update_intern(callback.message.chat.id, study_duration=duration)
    duration_info = STUDY_DURATIONS.get(str(duration), {})
    await callback.answer(t('update.saved', lang))
    await callback.message.edit_text(
        f"✅ {t('update.duration_changed', lang)}: {duration_info.get('emoji', '')} *{duration_info.get('name', '')}*\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()

@settings_router.message(UpdateStates.updating_schedule)
async def on_save_schedule(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru')
    try:
        h, m = map(int, message.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except:
        await message.answer(t('errors.try_again', lang) + " (HH:MM)")
        return

    normalized_time = f"{h:02d}:{m:02d}"
    await update_intern(message.chat.id, schedule_time=normalized_time)
    await message.answer(
        f"✅ {t('update.schedule_changed', lang)}: *{normalized_time}*\n\n"
        f"{t('commands.learn', lang)}\n"
        f"{t('commands.update', lang)}",
        parse_mode="Markdown"
    )
    await state.clear()
