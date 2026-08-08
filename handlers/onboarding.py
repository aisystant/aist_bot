"""
Хендлеры онбординга — /start и все шаги регистрации.

Использует legacy aiogram FSM (OnboardingStates).
Вся логика собрана здесь, bot.py только импортирует OnboardingStates.
"""

import asyncio
import logging
import os
import re
from datetime import timedelta

from core.tracing import span

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
from db.queries.users import moscow_today, get_slot_load, MAX_USERS_PER_SLOT, is_onboarded, coerce_ui_lang
from i18n import t, detect_language, get_language_name, SUPPORTED_LANGUAGES
from integrations.telegram.keyboards import (
    kb_study_duration, kb_complexity_level, kb_marathon_start, kb_confirm, kb_learn, kb_language_select,
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
    waiting_for_complexity_level = State()  # 2b. Сложность контента (WP-330 С9a)
    waiting_for_schedule = State()       # 3. Время напоминания
    waiting_for_start_date = State()     # 4. Дата старта марафона
    confirming_profile = State()
    choosing_path = State()              # 5. Выбор пути: Марафон / Аккаунт (WP-330)
    marathon_awaiting_time = State()     # 6. Ввод времени доставки уроков (WP-330)


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


async def _save_entry_source_from_deeplink(chat_id: int, raw_source: str, intern: dict) -> None:
    """Сохранить источник входа из deep-link `/start src_<value>` (WP-406 Ф16-B3).

    Нормализованное значение (site|stand|bot|guide-kit) кладётся в
    current_context['onboarding']['entry_source'] через канонический writer
    Онбордера; локальная копия intern обновляется, чтобы последующие ветки
    cmd_start (update_intern по stale current_context) не затёрли отметку.
    Fail-open: ошибка сохранения не ломает /start.
    """
    try:
        from core.onboarder import normalize_entry_source
        from core.onboarder import storage as onboarder_storage
        entry_source = normalize_entry_source(raw_source)
        onboarding_ctx = await onboarder_storage.save_onboarding_context(
            chat_id, {"entry_source": entry_source}
        )
        if intern is not None:
            ctx = intern.get("current_context") or {}
            ctx["onboarding"] = onboarding_ctx
            intern["current_context"] = ctx
    except Exception as e:
        logger.warning("[onboarding] entry_source deep link save failed for %s: %s", chat_id, e)


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

    # Single DB load — reused across all deep-link branches (latency fix, WP- peer-session)
    _uid = message.from_user.id if message.from_user else message.chat.id
    # WP-330 (peer-session 2026-06-05-34): span на первичную загрузку intern.
    async with span("start.get_intern"):
        intern = await get_intern(_uid)

    # Deep link: /start src_<source> → метка источника входа в онбординг
    # (WP-406 Ф16-B3, MVP). Значения: site | stand | bot | guide-kit, дефолт bot.
    # Не прерывает поток — /start продолжается как обычно (fall through).
    if len(args) > 1 and args[1].startswith("src_"):
        await _save_entry_source_from_deeplink(_uid, args[1][4:], intern)

    # Deep links: /start consent | consent_optout | consent_revoke
    if len(args) > 1 and args[1] in ("consent", "consent_optout", "consent_revoke"):
        if intern and intern.get('onboarding_completed'):
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

    _UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

    # Deep link: /start invite_<code> → постоянная реферальная ссылка WP-266.
    if len(args) > 1 and args[1].startswith("invite_"):
        invite_code = args[1][7:]
        if intern and intern.get("dt_user_id"):
            from handlers.referral import activate_invite_for_user
            await activate_invite_for_user(message, intern["dt_user_id"], invite_code)
            return
        if invite_code and len(invite_code) <= 64:
            await message.answer(
                "👋 <b>Тебя пригласили в Aisystant.</b>\n\n"
                "Пройди короткую регистрацию. Тексты руководств останутся доступны навсегда.",
                parse_mode="HTML",
            )
            ctx = (intern or {}).get("current_context", {}) or {}
            ctx["invite_code"] = invite_code
            await update_intern(_uid, current_context=ctx)

    # Deep link: /start ref_<ory_uuid> → реферальный онбординг (Ф20, WP-349)
    if len(args) > 1 and args[1].startswith("ref_"):
        ref_uuid = args[1][4:]  # убираем "ref_" prefix
        if ref_uuid and _UUID_RE.match(ref_uuid.lower()):
            if intern and intern.get('onboarding_completed'):
                pass  # онбордированный: fall through к обычному /start
            else:
                # Новый пользователь: показать generic welcome + сохранить ref
                await message.answer(
                    "👋 <b>Привет!</b>\n\n"
                    "Тебя пригласил друг из сообщества Aisystant.\n"
                    "Сейчас пройдём короткую регистрацию — займёт пару минут.",
                    parse_mode="HTML",
                )
                # Сохранить referral_uuid в current_context для consent_accept
                ctx = (intern or {}).get('current_context', {}) or {}
                ctx['referral_uuid'] = ref_uuid
                await update_intern(_uid, current_context=ctx)
                # Fall through: продолжаем обычный онбординг

    # Deep link: /start marathon → запуск марафона напрямую (Ф18, WP-349)
    if len(args) > 1 and args[1] == "marathon":
        if intern and intern.get('onboarding_completed'):
            from handlers.marathon import start_marathon_flow
            await start_marathon_flow(_uid, message)
            return
        # New user: fall through to onboarding, marathon starts after

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
            # WP-330: span на вход в mode_select (detect_ui_tier: sequential aisystant_id → parallel sub/github/dt).
            async with span("start.route_mode"):
                await dispatcher.route_command('mode', intern)

            # Напоминание о привязке Aisystant, если не привязан
            from db.queries.aisystant import get_aisystant_id
            lang = intern.get('language', 'ru') or 'ru'
            async with span("start.link_lookup"):
                aisystant_id = await get_aisystant_id(message.chat.id)
            if not aisystant_id:
                await message.answer(t('welcome.link_reminder', lang), parse_mode="Markdown")

            # Sync per-user menu commands (hamburger)
            from core.tier_ui import sync_menu_commands
            tier_str = intern.get('tier')
            if tier_str and tier_str.startswith('T'):
                tier = int(tier_str[1:])
            else:
                from core.tier_detector import detect_ui_tier
                async with span("start.tier"):
                    tier = await detect_ui_tier(message.chat.id)
            asyncio.create_task(sync_menu_commands(message.bot, message.chat.id, tier, lang))

            # WP-406 Ф5: вход Онбордера для вернувшихся с незакрытым Х2/Х3
            async with span("start.onboarder_offer"):
                await _maybe_offer_onboarder(message, message.chat.id)
            return

        lang = intern.get('language', 'ru')

        # Определяем текущий режим
        from config import Mode
        current_mode = intern.get('mode', Mode.MARATHON)
        mode_emoji = "🏃" if current_mode == Mode.MARATHON else "📚"
        mode_name = t('help.marathon', lang) if current_mode == Mode.MARATHON else t('help.feed', lang)

        # Прогресс активности
        from db.queries.activity import get_activity_stats
        from core.topics import get_marathon_day, get_display_day
        async with span("start.activity"):
            stats = await get_activity_stats(message.chat.id)
        total_active = stats.get('total', 0)
        marathon_day = get_marathon_day(intern)
        display_day = get_display_day(intern)

        # Send welcome with tier-based ReplyKeyboard (WP-52)
        from core.tier_ui import build_reply_keyboard, sync_menu_commands
        tier_str = intern.get('tier')
        if tier_str and tier_str.startswith('T'):
            tier = int(tier_str[1:])
        else:
            from core.tier_detector import detect_ui_tier
            async with span("start.tier"):
                tier = await detect_ui_tier(message.chat.id)
        keyboard = build_reply_keyboard(tier, lang)

        # Напоминание о привязке Aisystant, если не привязан
        from db.queries.aisystant import get_aisystant_id
        async with span("start.link_lookup"):
            aisystant_id = await get_aisystant_id(message.chat.id)

        text = (
            t('welcome.returning', lang, name=intern['name']) + "\n" +
            f"{mode_emoji} {t('welcome.current_mode', lang)}: *{mode_name}*\n" +
            f"📊 {t('welcome.activity_progress', lang)}: {total_active} {t('shared.of', lang)} {display_day}"
        )
        if not aisystant_id:
            text += "\n\n" + t('welcome.link_reminder', lang)

        async with span("start.welcome"):
            await message.answer(
                text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        # Sync per-user menu commands (fire-and-forget to reduce latency)
        asyncio.create_task(sync_menu_commands(message.bot, message.chat.id, tier, lang))

        # WP-406 Ф5: вход Онбордера для вернувшихся с незакрытым Х2/Х3
        async with span("start.onboarder_offer"):
            await _maybe_offer_onboarder(message, message.chat.id)
        return

    # WP-79: Упрощённый онбординг — 0 шагов
    # Язык автоопределяем из Telegram, имя берём из first_name
    lang = detect_language(message.from_user.language_code)
    if lang not in SUPPORTED_LANGUAGES:
        lang = 'en'
    lang = coerce_ui_lang(lang)  # WP-440: pin onboarding to Russian on track A

    name = message.from_user.first_name or message.from_user.username or 'User'

    # Сразу создаём профиль и завершаем онбординг
    async with span("start.profile_create"):
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
    async with span("start.auto_link"):
        aisystant_id = await _try_auto_link(message.chat.id)
    linked = aisystant_id is not None

    # Отправляем приветствие + T0 клавиатуру
    from core.tier_ui import build_reply_keyboard, sync_menu_commands
    from core.tier_detector import detect_ui_tier
    async with span("start.tier"):
        tier = await detect_ui_tier(message.chat.id)
    keyboard = build_reply_keyboard(tier, lang)

    if not linked:
        # Экран A — аккаунт не привязан → выбор пути (WP-330)
        # РП406 Ф30: строка доверия сразу на первом экране — Экран B (linked)
        # уже проактивно показывает её через consent-флоу (handlers/consent.py
        # _privacy_text), здесь такого шага нет, поэтому добавляем прямо тут.
        await message.answer(
            f"Привет, <b>{name}</b>! 👋\n\n"
            "Добро пожаловать в IWE — среду для работы и развития.\n\n"
            "Кстати: мы сохраняем переписку, чтобы помнить контекст, и не "
            "передаём данные в рекламных целях. Подробнее — /privacy.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await message.answer(
            "С чего начнём?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Марафон систематичности", callback_data="path_marathon")],
                [InlineKeyboardButton(text="🔗 У меня есть аккаунт", callback_data="onboarding_link_start")],
            ]),
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
        # WP-406 Ф5: вход Онбордера сразу после привязки (Экран B быстрого пути)
        await _maybe_offer_onboarder(message, message.chat.id)

    async with span("start.menu_sync"):
        await sync_menu_commands(message.bot, message.chat.id, tier, lang)

    # WP-151 Ф3 / WP-406 Ф18: registration_completed (fast path).
    # Renamed from 'onboarding_completed' — that name is now reserved for the
    # moment both Х2 and Х3 close (see core/onboarder/x2.py, on_x3_confirm below).
    from db.queries.events import log_event
    from db.queries.onboarding_journey import get_cohort_id_for_chat
    cohort_id = 'R1'
    async with span("start.registration_event"):
        if aisystant_id:
            cohort_id = await get_cohort_id_for_chat(message.chat.id)
        await log_event(message.chat.id, 'registration_completed', {
            'lang': lang,
            'path': 'fast',
            'linked_aisystant': linked,
            'cohort_id': cohort_id,
        })

    if linked:
        await state.clear()
    else:
        await state.set_state(OnboardingStates.choosing_path)


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
        t('onboarding.ask_complexity', lang) + "\n\n" +
        t('onboarding.ask_complexity_hint', lang),
        parse_mode="Markdown",
        reply_markup=kb_complexity_level(lang)
    )
    await state.set_state(OnboardingStates.waiting_for_complexity_level)

    # WP-151 Ф3: onboarding_step
    from db.queries.events import log_event
    await log_event(callback.message.chat.id, 'onboarding_step', {'step': 'duration'})


@onboarding_router.callback_query(OnboardingStates.waiting_for_complexity_level, F.data.startswith("complexity_"))
async def on_complexity(callback: CallbackQuery, state: FSMContext):
    """WP-330 С9a: пилот выбирает уровень сложности контента (1 или 2)."""
    lang = await get_lang(state)
    level = int(callback.data.replace("complexity_", ""))
    await update_intern(callback.message.chat.id, complexity_level=level)
    await callback.answer()
    await callback.message.edit_text(
        t('onboarding.ask_time', lang) + "\n\n" +
        t('onboarding.ask_time_hint', lang),
        parse_mode="Markdown"
    )
    await state.set_state(OnboardingStates.waiting_for_schedule)

    from db.queries.events import log_event
    await log_event(callback.message.chat.id, 'onboarding_step', {'step': 'complexity'})

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

        from core.topics import get_marathon_day, get_display_day
        marathon_day = get_marathon_day(intern)
        display_day = get_display_day(intern)
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
                start_msg = f"🗓 *{t('progress.day', lang, day=display_day, total=MARATHON_DAYS)}*"
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
                callback_data="start_onboarder_x3",
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

        # WP-151 Ф3 / WP-406 Ф18: onboarding_step + registration_completed (FSM path).
        # Renamed from 'onboarding_completed' — see rationale at the fast-path call above.
        from db.queries.events import log_event
        from db.queries.aisystant import get_aisystant_id
        from db.queries.onboarding_journey import get_cohort_id_for_chat
        _fsm_aisystant_id = await get_aisystant_id(chat_id)
        _fsm_cohort_id = 'R1'
        if _fsm_aisystant_id:
            _fsm_cohort_id = await get_cohort_id_for_chat(chat_id)
        await log_event(chat_id, 'onboarding_step', {'step': 'confirm'})
        await log_event(chat_id, 'registration_completed', {
            'lang': lang,
            'path': 'fsm',
            'duration': intern.get('study_duration'),
            'schedule_time': intern.get('schedule_time'),
            'start_date': str(intern.get('marathon_start_date')),
            'linked_aisystant': _fsm_aisystant_id is not None,
            'cohort_id': _fsm_cohort_id,
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


# ============= WP-406: ОНБОРДЕР Х3 — ВЫБОР ТРАЕКТОРИИ =============

@onboarding_router.callback_query(F.data == "start_onboarder_x3")
async def on_start_onboarder_x3(callback: CallbackQuery):
    """WP-406 Ф5: Запуск Х3 (выбор траектории) через Онбордера."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    if not intern:
        return
    try:
        from core.onboarder.x3 import run_x3
        await run_x3(intern, callback.message)
    except Exception as e:
        logger.error("[onboarder_x3] run_x3 failed for %s: %s", chat_id, e)
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.processing_error', lang))


@onboarding_router.callback_query(F.data.startswith("x3_confirm:"))
async def on_x3_confirm(callback: CallbackQuery):
    """WP-406 Ф5: Пользователь подтвердил выбор курса — закрыть Х3.

    Защита от повторного клика по устаревшей кнопке (WP-406 Ф18 backlog,
    найдено 2026-07-11 живым дублем в проде): если Х3 уже закрыт — событие
    не логируем повторно, симметрично _finish_x2 в core/onboarder/x2.py.
    """
    await callback.answer()
    chat_id = callback.from_user.id
    try:
        # Format: x3_confirm:<stream>:<diagnostic_flag> where flag "1"=bridge, "0"=direct, absent=legacy
        parts = callback.data.split(":", 2)
        chosen_stream = parts[1] if len(parts) > 1 else ""
        diagnostic_done = (parts[2] == "1") if len(parts) > 2 else False
        from core.onboarder import storage
        status = await storage.get_status(chat_id)
        if status["x3_done"]:
            return
        _x2_done_before = status["x2_done"]
        await storage.mark_x3_done(chat_id)
        if chosen_stream:
            await storage.save_onboarding_context(chat_id, {"confirmed_stream": chosen_stream})
        # WP-406 Ф17 PR-2: x3_completed event (fire after mark; diagnostic_done from callback flag)
        from core.onboarder import normalize_entry_source
        from db.queries.events import log_event
        _onb_ctx = await storage.get_onboarding_context(chat_id)
        _intern = await get_intern(chat_id)
        _lang = (_intern.get("language", "ru") or "ru") if _intern else "ru"
        _entry_type = _onb_ctx.get("entry_type", "direct")
        # WP-406 Ф16-B3: source = канал входа (site|stand|bot|guide-kit), дефолт bot
        _entry_source = normalize_entry_source(_onb_ctx.get("entry_source"))
        await log_event(chat_id, "x3_completed", {
            "entry_type": _entry_type,
            "source": _entry_source,
            "lang": _lang,
            "diagnostic_done": diagnostic_done,
            "stream": chosen_stream,
        })
        # WP-406 Ф18: Первокурсник достигнут = Х2 и Х3 оба закрыты. Х3 закрывается здесь;
        # если Х2 был закрыт раньше — это последний из двух разрывов, событие логируется тут.
        # Симметричный лог для обратного порядка — core/onboarder/x2.py:_finish_x2.
        if _x2_done_before:
            await log_event(chat_id, "onboarding_completed", {
                "entry_type": _entry_type,
                "source": _entry_source,
                "lang": _lang,
                "closed_by": "x3",
            })
            # WP-406 Ф31: дефолтная квалификация «Ученик», если своей ещё нет.
            # Fail-open: ошибка записи не ломает онбординг.
            try:
                from db.queries.dt_sync import ensure_default_qualification
                await ensure_default_qualification(chat_id)
            except Exception as e:
                logger.error(
                    "[onboarder_x3] default qualification assignment failed for %s: %s",
                    chat_id, e,
                )
        await callback.message.answer("✅ Курс выбран! Добро пожаловать в программу.")
    except Exception as e:
        logger.error("[onboarder_x3] mark_x3_done failed for %s: %s", chat_id, e)
        intern = await get_intern(chat_id)
        lang = (intern.get('language', 'ru') or 'ru') if intern else 'ru'
        await callback.message.answer(t('errors.processing_error', lang))


@onboarding_router.callback_query(F.data == "start_diagnose_for_x3")
async def on_start_diagnose_for_x3(callback: CallbackQuery, state: FSMContext):
    """WP-406 Ф5: Уточнить курс через Диагноста — поставить мост return_to и запустить /diagnose."""
    await callback.answer()
    chat_id = callback.from_user.id
    try:
        import datetime as _dt
        from core.onboarder.storage import save_onboarding_context
        from core.onboarder.x3 import _RETURN_TO_X3
        await save_onboarding_context(chat_id, {
            "return_to": _RETURN_TO_X3,
            "set_at": _dt.datetime.utcnow().isoformat(),
        })
        from handlers.diagnose import cmd_diagnose
        await cmd_diagnose(callback.message, state)
    except Exception as e:
        logger.error("[onboarder_x3] start_diagnose_for_x3 failed for %s: %s", chat_id, e)
        try:
            from core.onboarder.storage import save_onboarding_context
            await save_onboarding_context(chat_id, {"return_to": None, "set_at": None})
        except Exception as clear_e:
            logger.debug("[onboarder_x3] failed to clear stale return_to for %s: %s", chat_id, clear_e)
        intern = await get_intern(chat_id)
        lang = (intern.get('language', 'ru') or 'ru') if intern else 'ru'
        await callback.message.answer(t('errors.processing_error', lang))


# ============= WP-406: ОНБОРДЕР — ЕДИНЫЙ ВХОД + Х2 (ПОНИМАНИЕ СООБЩЕСТВА) =============

async def _maybe_offer_onboarder(message: Message, chat_id: int) -> None:
    """Показать кнопку «Освоиться» (вход Онбордера), если есть открытый разрыв Х2/Х3.

    Точка достижимости: вызывается там, куда новый человек реально попадает после
    /start (Экран B быстрого пути) и в приветствии возвращающегося. Решение «что и
    когда» — в core/onboarder/offer.py (should_offer = разрыв + cooldown); здесь
    только отрисовка кнопки.
    """
    from core.onboarder import offer
    try:
        if not await offer.should_offer(chat_id):
            return
    except Exception as e:
        logger.warning("[onboarder] should_offer check failed for %s: %s", chat_id, e)
        return
    payload = offer.offer_payload()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=payload["button_text"], callback_data=payload["callback_data"]),
    ]])
    await message.answer(payload["text"], reply_markup=kb)
    try:
        await offer.mark_offered(chat_id)
    except Exception as e:
        logger.warning("[onboarder] failed to record offer timestamp for %s: %s", chat_id, e)


@onboarding_router.callback_query(F.data == "onboarder_start")
async def on_onboarder_start(callback: CallbackQuery):
    """WP-406 Ф5: единый вход Онбордера — довести первый открытый разрыв (Х2 → Х3)."""
    await callback.answer()
    chat_id = callback.from_user.id
    # Гасим кнопку сразу: повторный тап по тому же сообщению не должен
    # запускать handle() заново (инцидент 2026-07-10/11 — см. core/onboarder/x2.py).
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logger.debug("[onboarder] could not clear offer button for %s: %s", chat_id, e)
    async with span("onboarder.get_intern"):
        intern = await get_intern(chat_id)
    if not intern:
        return
    try:
        from core.onboarder import handle
        async with span("onboarder.handle"):
            await handle(intern, callback.message)
    except Exception as e:
        logger.error("[onboarder] handle failed for %s: %s", chat_id, e)
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.processing_error', lang))


@onboarding_router.callback_query(F.data.startswith("x2_confirm:"))
async def on_x2_confirm(callback: CallbackQuery):
    """WP-406 Ф5: пользователь подтвердил пункт понимания сообщества → следующий шаг."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    if not intern:
        return
    topic = callback.data.split(":", 1)[1] if ":" in callback.data else ""
    try:
        from core.onboarder import x2
        await x2.confirm_topic(intern, callback.message, topic)
    except Exception as e:
        logger.error("[onboarder_x2] confirm_topic failed for %s (%s): %s", chat_id, topic, e)
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.processing_error', lang))


@onboarding_router.callback_query(F.data.startswith("x2_more:"))
async def on_x2_more(callback: CallbackQuery):
    """WP-406 Ф5: «Подробнее» по пункту понимания сообщества."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    if not intern:
        return
    topic = callback.data.split(":", 1)[1] if ":" in callback.data else ""
    try:
        from core.onboarder import x2
        await x2.show_more(intern, callback.message, topic)
    except Exception as e:
        logger.error("[onboarder_x2] show_more failed for %s (%s): %s", chat_id, topic, e)
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.processing_error', lang))


# ============= WP-330: ПУТЬ К МАРАФОНУ =============

@onboarding_router.callback_query(F.data == "path_marathon")
async def on_path_marathon(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал марафон — показываем экран выбора времени."""
    await callback.answer()
    await callback.message.edit_text(
        "📚 <b>Марафон систематичности</b>\n\n"
        "Ставим стиль саморазвития: 14 дней × ~15 мин/день.\n"
        "Теория + практика + вечерний чек-ин каждый день.\n\n"
        "Сообщение с занятием запланировано на 04:00 МСК.\n"
        "Хочешь другое время — введи под сообщением время в формате ЧЧ:ММ",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Начать в 04:00", callback_data="marathon_start_default")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="path_back")],
        ]),
    )
    await state.set_state(OnboardingStates.marathon_awaiting_time)


@onboarding_router.callback_query(F.data == "path_back")
async def on_path_back(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору пути."""
    await callback.answer()
    await callback.message.edit_text(
        "С чего начнём?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Марафон систематичности", callback_data="path_marathon")],
            [InlineKeyboardButton(text="🔗 У меня есть аккаунт", callback_data="onboarding_link_start")],
        ]),
    )
    await state.set_state(OnboardingStates.choosing_path)


@onboarding_router.callback_query(F.data == "marathon_start_default")
async def on_marathon_start_default(callback: CallbackQuery, state: FSMContext):
    """Старт марафона с временем по умолчанию (04:00 МСК)."""
    await callback.answer()
    chat_id = callback.from_user.id
    await update_intern(chat_id, schedule_time="04:00")
    await state.clear()
    from handlers.marathon import start_marathon_flow
    await start_marathon_flow(chat_id, callback.message, schedule_time="04:00")


@onboarding_router.message(OnboardingStates.marathon_awaiting_time)
async def on_marathon_time_input(message: Message, state: FSMContext):
    """Пользователь ввёл своё время доставки уроков."""
    try:
        h, m = map(int, message.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except Exception:
        await message.answer("Введи время в формате ЧЧ:ММ, например «09:00»")
        return
    schedule_time = f"{h:02d}:{m:02d}"
    await update_intern(message.chat.id, schedule_time=schedule_time)
    await state.clear()
    from handlers.marathon import start_marathon_flow
    await start_marathon_flow(message.chat.id, message, schedule_time=schedule_time)


# ============= WP-79: AUTO-LINK AISYSTANT =============

async def _try_auto_link(chat_id: int) -> str | None:
    """Попытка автоматической привязки Aisystant аккаунта при /start.

    Returns Aisystant numeric/string ID if linked, None otherwise.
    """
    try:
        from db.queries.aisystant import get_aisystant_id, save_aisystant_link
        from clients.aisystant import aisystant

        # Уже привязан?
        existing = await get_aisystant_id(chat_id)
        if existing:
            return existing

        # Пробуем найти
        aisystant_id = await aisystant.find_user_by_tg(chat_id)
        if aisystant_id:
            await save_aisystant_link(chat_id, aisystant_id)
            logger.info(f"[Onboarding] Auto-linked Aisystant for {chat_id}: {aisystant_id}")
            return aisystant_id
        return None
    except Exception as e:
        logger.debug(f"[Onboarding] Auto-link failed for {chat_id}: {e}")
        return None
