"""
Хендлеры онбординга — /start и все шаги регистрации.

Использует legacy aiogram FSM (OnboardingStates).
Вся логика собрана здесь, bot.py только импортирует OnboardingStates.
"""

import logging
import os
from datetime import timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


async def _personal_guide_button(chat_id: int) -> list[InlineKeyboardButton] | None:
    """Inline-кнопка для доступа к личному руководству (WP-301 Ф7, WP-309 Ф9.2).

    T3a (managed): пилот без GitHub-аккаунта — кнопка «📖 Моё руководство» → web-ридер.
    T3b (sovereign): пилот с GitHub App — кнопка «📚 Подключи личное руководство».

    T3a показывается при наличии consent + записи в pilot_repo_map.
    T3b показывается при наличии consent + GITHUB_APP_SLUG env.
    Returns None если ни один вариант недоступен.
    """
    from helpers.dual_write import resolve_ory_id_from_chat
    from db.queries.consent import get_consent
    try:
        account_id = await resolve_ory_id_from_chat(chat_id)
        if not account_id:
            return None
        consent = await get_consent(account_id)
        if not consent or not consent["opt_in"]:
            return None
    except Exception:
        return None

    # T3a: managed-репо уже создано — показать прямую ссылку на web-ридер
    guide_web_url = os.getenv("GUIDE_WEB_URL", "https://guide.system-school.ru").rstrip("/")
    try:
        from db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM learning.pilot_repo_map WHERE pilot_uuid = $1",
                account_id,
            )
        if row is not None:
            return [InlineKeyboardButton(
                text="📖 Моё руководство",
                url=f"{guide_web_url}/guide/{account_id}",
            )]
    except Exception:
        pass  # fallback to T3b below

    # T3b: sovereign — предложить подключить GitHub App
    app_slug = os.getenv("GITHUB_APP_SLUG", "").strip()
    webhook_url = os.getenv("WEBHOOK_URL", "").rstrip("/")
    if not app_slug or not webhook_url:
        return None
    setup_url = f"{webhook_url}/auth/github_app/setup?telegram_user_id={chat_id}"
    return [InlineKeyboardButton(
        text="📚 Подключи личное руководство",
        url=setup_url,
    )]

from config import STUDY_DURATIONS, MARATHON_DAYS, MarathonStatus
from db.queries import get_intern, update_intern
from db.queries.users import moscow_today, get_slot_load, MAX_USERS_PER_SLOT, is_onboarded
from i18n import t, detect_language, get_language_name, SUPPORTED_LANGUAGES
from integrations.telegram.keyboards import (
    kb_study_duration, kb_marathon_start, kb_confirm, kb_learn, kb_language_select,
    kb_slot_suggestions,
)

logger = logging.getLogger(__name__)

onboarding_router = Router(name="onboarding")


# ============= СОСТОЯНИЯ FSM =============

class OnboardingStates(StatesGroup):
    """Онбординг для марафона (slim: имя → длительность → расписание → старт)"""
    choosing_language = State()          # 0. Язык (для неподдерживаемых языков)
    waiting_for_name = State()           # 1. Имя
    waiting_for_study_duration = State() # 2. Время на тему
    waiting_for_schedule = State()       # 3. Время напоминания
    waiting_for_start_date = State()     # 4. Дата старта марафона
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
    """Есть ли у пользователя реальный учебный прогресс.

    Проверяем только фактически пройденные темы, НЕ статус марафона/ленты.
    Пользователь с marathon_status='active' но 0 пройденных тем
    не имеет данных для сброса.
    """
    completed = intern.get('completed_topics') or []
    return len(completed) > 0


# ============= ХЕНДЛЕРЫ =============

@onboarding_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    args = message.text.split(maxsplit=1)

    # Deep link: /start seminar_{code} → карточка семинара
    if len(args) > 1 and args[1].startswith("seminar_"):
        try:
            seminar_code = args[1].split("_", 1)[1]
            if seminar_code:
                from handlers.showcase import _show_seminar_card
                await _show_seminar_card(message, seminar_code)
                return
        except (ValueError, IndexError):
            pass

    # Deep links: /start consent | consent_optout | consent_revoke
    if len(args) > 1 and args[1] in ("consent", "consent_optout", "consent_revoke"):
        _uid = message.from_user.id if message.from_user else message.chat.id
        intern_check = await get_intern(_uid)
        if intern_check and intern_check.get('onboarding_completed'):
            if args[1] == "consent":
                from handlers.consent import show_consent_optin
                await show_consent_optin(message)
            elif args[1] == "consent_optout":
                from handlers.consent import show_consent_optout
                await show_consent_optout(message)
            elif args[1] == "consent_revoke":
                from handlers.consent import show_consent_revoke
                await show_consent_revoke(message)
            return
        # New user: fall through to normal onboarding; consent button shown after auto-link

    # Deep link: /start marathon → запуск марафона напрямую (Ф18, WP-349)
    if len(args) > 1 and args[1] == "marathon":
        _uid = message.from_user.id if message.from_user else message.chat.id
        intern_check = await get_intern(_uid)
        if intern_check and intern_check.get('onboarding_completed'):
            from handlers.marathon import start_marathon_flow
            await start_marathon_flow(_uid, message)
            return
        # New user: fall through to onboarding, marathon starts after

    intern = await get_intern(message.chat.id)

    if intern['onboarding_completed']:
        # Очищаем legacy FSM state
        await state.clear()

        # Очищаем stale settings_waiting_for (WP-60 bugfix)
        ctx = intern.get('current_context', {})
        if ctx.get('settings_waiting_for'):
            ctx.pop('settings_waiting_for')
            await update_intern(message.chat.id, current_context=ctx)
            intern['current_context'] = ctx

        # Проверяем наличие старых учебных данных → предлагаем сброс (одноразово)
        reset_offered = ctx.get('reset_offered', False)
        if not reset_offered and _has_learning_data(intern):
            lang = intern.get('language', 'ru')
            completed_count = len(intern.get('completed_topics', []))
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('reset.go_mydata_btn', lang),
                    callback_data="reset_go_mydata",
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
        # WP-52: mode_select now sends tier-based ReplyKeyboard directly
        from handlers import get_dispatcher
        dispatcher = get_dispatcher()
        if dispatcher and dispatcher.is_sm_active:
            await dispatcher.route_command('mode', intern)

            # Напоминание о привязке Aisystant, если не привязан
            from db.queries.aisystant import get_aisystant_id
            lang = intern.get('language', 'ru') or 'ru'
            aisystant_id = await get_aisystant_id(message.chat.id)
            if not aisystant_id:
                await message.answer(t('welcome.link_reminder', lang), parse_mode="Markdown")

            # Sync per-user menu commands (hamburger)
            from core.tier_ui import sync_menu_commands
            from core.tier_detector import detect_ui_tier
            tier = await detect_ui_tier(message.chat.id)
            await sync_menu_commands(message.bot, message.chat.id, tier, lang)
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
        tier = await detect_ui_tier(message.chat.id)
        keyboard = build_reply_keyboard(tier, lang)

        # Напоминание о привязке Aisystant, если не привязан
        from db.queries.aisystant import get_aisystant_id
        aisystant_id = await get_aisystant_id(message.chat.id)

        text = (
            t('welcome.returning', lang, name=intern['name']) + "\n" +
            f"{mode_emoji} {t('welcome.current_mode', lang)}: *{mode_name}*\n" +
            f"📊 {t('welcome.activity_progress', lang)}: {total_active} {t('shared.of', lang)} {marathon_day}"
        )
        if not aisystant_id:
            text += "\n\n" + t('welcome.link_reminder', lang)

        await message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

        # Sync per-user menu commands
        await sync_menu_commands(message.bot, message.chat.id, tier, lang)
        return

    # WP-79: Упрощённый онбординг — 0 шагов
    # Язык автоопределяем из Telegram, имя берём из first_name
    lang = detect_language(message.from_user.language_code)
    if lang not in SUPPORTED_LANGUAGES:
        lang = 'en'

    name = message.from_user.first_name or message.from_user.username or 'User'

    # Сразу создаём профиль и завершаем онбординг
    await update_intern(
        message.chat.id,
        name=name,
        language=lang,
        onboarding_completed=True,
    )
    # Сохраняем tg_username для привязки Aisystant
    if message.from_user.username:
        await update_intern(message.chat.id, tg_username=message.from_user.username)

    # Пробуем привязать Aisystant (синхронно, чтобы знать результат)
    linked = await _try_auto_link(message.chat.id)

    # Отправляем приветствие + T0 клавиатуру
    from core.tier_ui import build_reply_keyboard, sync_menu_commands
    from core.tier_detector import detect_ui_tier
    tier = await detect_ui_tier(message.chat.id)
    keyboard = build_reply_keyboard(tier, lang)

    if not linked:
        # Экран A — аккаунт не привязан
        await message.answer(
            f"Привет, <b>{name}</b>! 👋\n\n"
            "Добро пожаловать в Мастерскую инженеров-менеджеров. "
            "Этот бот — один из интерфейсов платформы. "
            "Здесь можно учиться, отслеживать прогресс и разворачивать "
            "полное рабочее окружение — шаг за шагом.\n\n"
            "Для начала свяжите аккаунт Aisystant — это позволит "
            "платформе отслеживать прогресс и персонализировать обучение.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await message.answer(
            "Нажмите кнопку ниже — откроется вход в Aisystant.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🔗 Связать аккаунт Aisystant",
                    callback_data="onboarding_link_start",
                ),
            ]]),
        )
    else:
        # Экран B — аккаунт привязан: приветствие + Экран Consent
        await message.answer(
            f"Привет, <b>{name}</b>! 👋\n\n"
            "Добро пожаловать в Мастерскую инженеров-менеджеров. "
            "Этот бот — один из интерфейсов платформы. "
            "Здесь можно учиться, отслеживать прогресс и разворачивать "
            "полное рабочее окружение — шаг за шагом.\n\n"
            "✅ <b>Аккаунт привязан!</b> Следующий шаг — согласие на трекинг развития.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        from handlers.consent import show_consent_optin
        await show_consent_optin(message)

    await sync_menu_commands(message.bot, message.chat.id, tier, lang)

    # WP-151 Ф3: onboarding_completed (fast path)
    from db.queries.events import log_event
    await log_event(message.chat.id, 'onboarding_completed', {
        'lang': lang,
        'path': 'fast',
        'linked_aisystant': linked,
    })

    await state.clear()


@onboarding_router.callback_query(F.data == "onboarding_link_start")
async def on_onboarding_link_start(callback: CallbackQuery) -> None:
    """Кнопка 'Связать аккаунт Aisystant' из Экрана A — запускает flow привязки."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    chat_id = callback.from_user.id

    from db.queries.aisystant import get_aisystant_id, save_aisystant_link
    from clients.aisystant import aisystant as _aisystant

    # Уже привязан (пользователь нажал повторно)?
    existing = await get_aisystant_id(chat_id)
    if existing:
        await callback.message.answer(
            "✅ <b>Аккаунт уже привязан!</b> Следующий шаг — согласие на трекинг развития.",
            parse_mode="HTML",
        )
        from handlers.consent import show_consent_optin
        await show_consent_optin(callback.message)
        return

    # Пробуем найти автоматически
    try:
        aisystant_id = await _aisystant.find_user_by_tg(chat_id)
    except Exception as e:
        logger.warning("[Onboarding] link_start find_user_by_tg %s: %s", chat_id, e)
        aisystant_id = None

    if aisystant_id:
        await save_aisystant_link(chat_id, aisystant_id)
        await callback.message.answer(
            "✅ <b>Аккаунт привязан!</b> Следующий шаг — согласие на трекинг развития.",
            parse_mode="HTML",
        )
        from handlers.consent import show_consent_optin
        await show_consent_optin(callback.message)
        return

    # Не найден — показываем ссылку для привязки через сайт
    tg_username = callback.from_user.username
    try:
        link_url = await _aisystant.get_link_url(chat_id, tg_username)
    except Exception as e:
        logger.error("[Onboarding] link_start get_link_url %s: %s", chat_id, e)
        await callback.message.answer("Ошибка получения ссылки. Попробуйте /link.")
        return

    await callback.message.answer(
        "Для привязки:\n\n"
        "1. Нажмите «Войти в Aisystant» — авторизуйтесь или создайте аккаунт\n"
        "2. Вернитесь сюда и нажмите «Проверить»",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Войти в Aisystant", url=link_url)],
            [InlineKeyboardButton(text="✅ Проверить", callback_data="link_check")],
        ]),
    )


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
        t('welcome.intro_marathon', lang_code) + "\n\n" +
        t('welcome.intro_tiers', lang_code) + "\n\n" +
        t('welcome.intro_start', lang_code) + "\n\n" +
        t('welcome.ask_name', lang_code),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_name)

    # WP-151 Ф3: onboarding_step
    from db.queries.events import log_event
    await log_event(callback.message.chat.id, 'onboarding_step', {'step': 'language'})


@onboarding_router.message(OnboardingStates.waiting_for_name)
async def on_name(message: Message, state: FSMContext):
    lang = await get_lang(state)
    name = message.text.strip()
    await update_intern(message.chat.id, name=name, language=lang)
    # Slim onboarding: имя → сразу duration (occupation/interests/values/goals → /profile позже)
    await message.answer(
        t('onboarding.nice_to_meet', lang, name=name) + "\n\n" +
        t('onboarding.ask_duration', lang),
        parse_mode="Markdown",
        reply_markup=kb_study_duration(lang)
    )
    await state.set_state(OnboardingStates.waiting_for_study_duration)

    # WP-151 Ф3: onboarding_step
    from db.queries.events import log_event
    await log_event(message.chat.id, 'onboarding_step', {'step': 'name'})

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

    # WP-151 Ф3: onboarding_step
    from db.queries.events import log_event
    await log_event(callback.message.chat.id, 'onboarding_step', {'step': 'duration'})

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

    # WP-151 Ф3: onboarding_step
    from db.queries.events import log_event
    await log_event(message.chat.id, 'onboarding_step', {'step': 'schedule'})


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

    from db.queries.users import derive_mode
    await update_intern(callback.message.chat.id,
                        marathon_start_date=start_date,
                        marathon_status=MarathonStatus.ACTIVE,
                        mode=derive_mode(MarathonStatus.ACTIVE, 'not_started'))
    await callback.answer()

    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'

    duration = STUDY_DURATIONS.get(str(intern['study_duration']), {})

    await callback.message.edit_text(
        f"📋 *{t('profile.your_profile', lang)}:*\n\n"
        f"👤 *{t('profile.name_label', lang)}:* {intern['name']}\n"
        f"{duration.get('emoji', '')} {duration.get('name', '')} {t('profile.per_topic', lang)}\n"
        f"⏰ {t('profile.reminder_at', lang)} {intern['schedule_time']}\n"
        f"🗓 {t('profile.marathon_start', lang)}: *{start_date.strftime('%d.%m.%Y')}*\n\n"
        f"{t('profile.all_correct', lang)}",
        parse_mode="Markdown",
        reply_markup=kb_confirm(lang)
    )
    await state.set_state(OnboardingStates.confirming_profile)

    # WP-151 Ф3: onboarding_step
    from db.queries.events import log_event
    await log_event(callback.message.chat.id, 'onboarding_step', {'step': 'start_date'})

@onboarding_router.callback_query(OnboardingStates.confirming_profile, F.data == "confirm")
async def on_confirm(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    try:
        await update_intern(
            chat_id,
            onboarding_completed=True,
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
            f"{start_msg}\n\n"
            f"{t('welcome.marathon_personalize_hint', lang)}",
            parse_mode="Markdown",
            reply_markup=kb_learn(lang)
        )

        # Send tier-based ReplyKeyboard + sync menu commands (WP-52)
        from core.tier_ui import send_tier_keyboard
        await send_tier_keyboard(callback.message, intern)

        # WP-156: Inline-кнопка «Помоги выбрать» → Навигатор
        # WP-301 Ф7: + proactive «Подключи личное руководство»
        nav_buttons_fsm = [[
            InlineKeyboardButton(
                text="🧭 " + t('onboarding.navigator_hint', lang),
                callback_data="start_navigator",
            )
        ]]
        guide_btn = await _personal_guide_button(chat_id)
        if guide_btn:
            nav_buttons_fsm.append(guide_btn)
        nav_kb = InlineKeyboardMarkup(inline_keyboard=nav_buttons_fsm)
        await callback.message.answer(
            t('onboarding.navigator_offer', lang),
            reply_markup=nav_kb,
        )

        # WP-151 Ф3: onboarding_step + onboarding_completed (FSM path)
        from db.queries.events import log_event
        await log_event(chat_id, 'onboarding_step', {'step': 'confirm'})
        await log_event(chat_id, 'onboarding_completed', {
            'lang': lang,
            'path': 'fsm',
            'duration': intern.get('study_duration'),
            'schedule_time': intern.get('schedule_time'),
            'start_date': str(intern.get('marathon_start_date')),
        })

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

@onboarding_router.callback_query(F.data == "reset_go_mydata")
async def on_reset_go_mydata(callback: CallbackQuery, state: FSMContext):
    """Перенаправление в /mydata для сброса вместо прямого reset."""
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

    # Перенаправляем в /mydata через dispatcher
    intern = await get_intern(chat_id)
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        await dispatcher.route_command('mydata', intern)


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


# ============= WP-156: NAVIGATOR FROM ONBOARDING =============

@onboarding_router.callback_query(F.data == "start_navigator")
async def on_start_navigator(callback: CallbackQuery, state: FSMContext):
    """WP-156: Запуск Навигатора через inline-кнопку после онбординга."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    if not intern:
        return

    from handlers import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        await state.clear()
        await dispatcher.route_command('navigator', intern)
    else:
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.processing_error', lang))


# ============= WP-79: AUTO-LINK AISYSTANT =============

async def _try_auto_link(chat_id: int) -> bool:
    """Попытка автоматической привязки Aisystant аккаунта при /start.

    Returns True if linked (already was or newly linked), False otherwise.
    """
    try:
        from db.queries.aisystant import get_aisystant_id, save_aisystant_link
        from clients.aisystant import aisystant

        # Уже привязан?
        existing = await get_aisystant_id(chat_id)
        if existing:
            return True

        # Пробуем найти
        aisystant_id = await aisystant.find_user_by_tg(chat_id)
        if aisystant_id:
            await save_aisystant_link(chat_id, aisystant_id)
            logger.info(f"[Onboarding] Auto-linked Aisystant for {chat_id}: {aisystant_id}")
            return True
        return False
    except Exception as e:
        logger.debug(f"[Onboarding] Auto-link failed for {chat_id}: {e}")
        return False
