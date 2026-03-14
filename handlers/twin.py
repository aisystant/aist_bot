"""
Хендлеры интеграции с Digital Twin.
"""

import logging

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

twin_router = Router(name="twin")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


def _profile_text(profile: dict, lang: str, intern: dict = None) -> str:
    """Формирует текст профиля Digital Twin.

    Fallback chain: indicators.IND.1.PREF (Aisystant) → 1_declarative (bot sync) → intern (bot DB).
    """
    degree = profile.get("degree", t('twin.not_set', lang))
    stage = profile.get("stage", t('twin.not_set', lang))

    # Source 1: indicators path (Aisystant platform writes here)
    indicators = profile.get("indicators", {})
    pref = indicators.get("IND.1.PREF", {}) if isinstance(indicators, dict) else {}
    pref = pref if isinstance(pref, dict) else {}

    # Source 2: declarative path (bot sync writes here)
    declarative = profile.get("1_declarative", {}) if isinstance(profile, dict) else {}
    goals_sec = (declarative.get("1_2_goals", {}) if isinstance(declarative, dict) else {}) or {}
    selfeval_sec = (declarative.get("1_3_selfeval", {}) if isinstance(declarative, dict) else {}) or {}

    # Merge with fallback chain
    objective = (
        pref.get("objective")
        or goals_sec.get("09_Цели обучения")
        or (intern.get('goals') if intern else None)
        or t('twin.not_set', lang)
    )

    roles_raw = (
        pref.get("role_set")
        or selfeval_sec.get("06_Роли")
        or (intern.get('role') if intern else None)
    )
    if isinstance(roles_raw, str):
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    elif isinstance(roles_raw, list):
        roles = roles_raw
    else:
        roles = []
    roles_text = ", ".join(roles) if roles else t('twin.not_set_plural', lang)

    time_budget = (
        pref.get("weekly_time_budget")
        or t('twin.not_set_m', lang)
    )

    return (
        f"*{t('twin.profile_title', lang)}*\n\n"
        f"*{t('twin.degree_label', lang)}:* {degree}\n"
        f"*{t('twin.stage_label', lang)}:* {stage}\n"
        f"*{t('twin.objective_label', lang)}:* {objective}\n"
        f"*{t('twin.roles_label', lang)}:* {roles_text}\n"
        f"*{t('twin.time_budget_label', lang)}:* {time_budget} {t('twin.hours_per_week', lang)}"
    )


@twin_router.message(Command("twin"))
async def cmd_twin(message: Message):
    """Команда для работы с Digital Twin."""
    from clients.digital_twin import digital_twin

    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    text = message.text or ""
    parts = text.strip().split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) > 1 else None
    arg = parts[2] if len(parts) > 2 else None

    is_connected = digital_twin.is_connected(telegram_user_id)

    if subcommand == "disconnect":
        if is_connected:
            digital_twin.disconnect(telegram_user_id)
            # Clear persistent flag
            try:
                from db import get_pool
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute('UPDATE public.users SET dt_connected_at = NULL WHERE telegram_id = $1', telegram_user_id)
            except Exception:
                pass
            await message.answer(t('twin.disconnected', lang))
        else:
            await message.answer(t('twin.not_connected', lang))
        return

    if not is_connected:
        auth_url, state = digital_twin.get_authorization_url(telegram_user_id)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('twin.btn_connect', lang), url=auth_url)]
        ])
        await message.answer(
            f"*{t('twin.connect_title', lang)}*\n\n"
            f"{t('twin.connect_desc', lang)}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    if subcommand == "objective" and arg:
        await message.answer(t('twin.saving_objective', lang))
        result = await digital_twin.set_learning_objective(telegram_user_id, arg)
        if result:
            await message.answer(t('twin.objective_updated', lang, objective=arg), parse_mode="Markdown")
        else:
            await message.answer(t('twin.objective_error', lang))
        return

    if subcommand == "roles":
        roles = await digital_twin.get_roles(telegram_user_id)
        if roles:
            roles_text = ", ".join(roles) if isinstance(roles, list) else str(roles)
            await message.answer(f"*{t('twin.roles_title', lang)}*\n{roles_text}", parse_mode="Markdown")
        else:
            await message.answer(t('twin.roles_empty', lang))
        return

    if subcommand == "degrees":
        degrees = await digital_twin.get_degrees(telegram_user_id)
        if degrees:
            # describe_by_path возвращает markdown-текст
            text = degrees if isinstance(degrees, str) else str(degrees)
            # Ограничить длину для TG (4096 символов)
            if len(text) > 4000:
                text = text[:4000] + "\n..."
            await message.answer(text, parse_mode="Markdown")
        else:
            await message.answer(t('twin.degrees_error', lang))
        return

    if subcommand == "insights":
        await _handle_insights(message, intern, lang)
        return

    # По умолчанию: показать профиль
    await message.answer(t('twin.loading_profile', lang))
    profile = await digital_twin.get_user_profile(telegram_user_id)

    if profile is None:
        await message.answer(t('twin.unavailable', lang))
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('twin.btn_insights', lang), callback_data="twin_insights")],
        [InlineKeyboardButton(text=t('twin.btn_update_profile', lang), callback_data="twin_profile")],
        [InlineKeyboardButton(text=t('twin.btn_degrees', lang), callback_data="twin_degrees")],
        [InlineKeyboardButton(text=t('twin.btn_disconnect', lang), callback_data="twin_disconnect")],
    ])

    await message.answer(_profile_text(profile, lang, intern=intern), parse_mode="Markdown", reply_markup=keyboard)


@twin_router.callback_query(F.data == "twin_profile")
async def callback_twin_profile(callback: CallbackQuery):
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    # Persistent check: dt_connected_at survives redeploy
    connected = digital_twin.is_connected(telegram_user_id)
    if not connected:
        try:
            from db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT dt_connected_at FROM public.users WHERE telegram_id = $1',
                    telegram_user_id,
                )
                if row and row['dt_connected_at'] is not None:
                    # DT was connected but tokens lost after redeploy
                    await callback.answer()
                    await callback.message.answer(
                        t('twin.reconnect_needed', lang),
                        parse_mode="Markdown",
                    )
                    return
        except Exception:
            pass
        await callback.answer(t('twin.not_connected_alert', lang), show_alert=True)
        return

    await callback.answer()

    profile = await digital_twin.get_user_profile(telegram_user_id)
    if profile is None:
        await callback.message.answer(t('twin.unavailable_short', lang))
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('twin.btn_degrees', lang), callback_data="twin_degrees")],
        [InlineKeyboardButton(text=t('twin.btn_disconnect', lang), callback_data="twin_disconnect")],
    ])

    await callback.message.answer(
        _profile_text(profile, lang, intern=intern), parse_mode="Markdown", reply_markup=keyboard,
    )


@twin_router.callback_query(F.data == "twin_degrees")
async def callback_twin_degrees(callback: CallbackQuery):
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not digital_twin.is_connected(telegram_user_id):
        await callback.answer(t('twin.not_connected_alert', lang), show_alert=True)
        return

    await callback.answer()

    degrees = await digital_twin.get_degrees(telegram_user_id)
    if degrees:
        text = degrees if isinstance(degrees, str) else str(degrees)
        if len(text) > 4000:
            text = text[:4000] + "\n..."
        await callback.message.answer(text, parse_mode="Markdown")
    else:
        await callback.message.answer(t('twin.degrees_error', lang))


@twin_router.callback_query(F.data == "twin_insights")
async def callback_twin_insights(callback: CallbackQuery):
    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    await callback.answer()
    await _handle_insights(callback.message, intern, lang)


@twin_router.callback_query(F.data == "twin_disconnect")
async def callback_twin_disconnect(callback: CallbackQuery):
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not digital_twin.is_connected(telegram_user_id):
        await callback.answer(t('twin.already_disconnected', lang), show_alert=True)
        return

    digital_twin.disconnect(telegram_user_id)
    # Clear persistent flag
    try:
        from db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute('UPDATE public.users SET dt_connected_at = NULL WHERE telegram_id = $1', telegram_user_id)
    except Exception:
        pass
    await callback.answer(t('twin.disconnected_alert', lang), show_alert=True)
    await callback.message.edit_text(
        t('twin.disconnected_desc', lang),
        parse_mode="Markdown"
    )


async def _handle_insights(message: Message, intern: dict, lang: str):
    """Генерирует AI-интерпретацию engagement данных из ЦД (Phase 5A)."""
    from db.queries.dt_sync import get_engagement_data
    from db.queries.dt_tokens import get_dt_user_id

    telegram_user_id = message.chat.id

    # Получить user_uuid для чтения digital_twins
    dt_user_id = await get_dt_user_id(telegram_user_id)
    if not dt_user_id:
        await message.answer(t('twin.insights_no_dt', lang))
        return

    await message.answer(t('twin.insights_loading', lang))

    engagement = await get_engagement_data(dt_user_id)
    if not engagement:
        await message.answer(t('twin.insights_no_data', lang))
        return

    # Собрать контекст для промпта
    name = (intern or {}).get('name', '')
    goals = (intern or {}).get('goals', '')
    occupation = (intern or {}).get('occupation', '')

    account = engagement.get('2_1_account', {})
    courses = engagement.get('2_2_courses', {})
    practice = engagement.get('2_3_practice', {})
    time_data = engagement.get('2_4_time', {})

    data_summary = (
        f"Sessions: {account.get('sessions_total', 0)}, "
        f"Events: {account.get('events_total', 0)}, "
        f"First activity: {account.get('first_event_at', 'N/A')}, "
        f"Last activity: {account.get('last_event_at', 'N/A')}\n"
        f"Marathon steps: {courses.get('marathon_steps_total', 0)}, "
        f"Feed digests: {courses.get('feed_completed_total', 0)}\n"
        f"Training attempts: {practice.get('training_attempts_total', 0)}, "
        f"Passed: {practice.get('training_passed_total', 0)}, "
        f"Assessments: {practice.get('assessments_total', 0)}, "
        f"Marathon tasks: {practice.get('marathon_tasks_total', 0)}\n"
        f"Active days: {time_data.get('active_days', 0)}, "
        f"Events last 7d: {time_data.get('events_last_7d', 0)}, "
        f"Events last 30d: {time_data.get('events_last_30d', 0)}, "
        f"AI chats: {time_data.get('ai_chats_total', 0)}"
    )

    lang_instruction = "Отвечай на русском." if lang == 'ru' else f"Answer in {lang}."

    system_prompt = (
        "You are a personal learning advisor analyzing a student's Digital Twin data. "
        "Give a brief, encouraging progress summary and ONE specific actionable recommendation. "
        "Be warm but honest. Use Telegram Markdown formatting (*bold*, _italic_). "
        f"Keep response under 200 words. {lang_instruction}"
    )

    user_prompt = (
        f"Student: {name}\n"
        f"{'Occupation: ' + occupation if occupation else ''}\n"
        f"{'Learning goals: ' + goals if goals else ''}\n\n"
        f"Engagement data:\n{data_summary}\n\n"
        "Analyze this data and provide:\n"
        "1. Brief progress summary (what's going well, what needs attention)\n"
        "2. One specific recommendation for the next step"
    )

    try:
        from bot import claude
        from config import CLAUDE_MODEL_HAIKU

        result = await claude.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=600,
            model=CLAUDE_MODEL_HAIKU,
        )

        if result:
            try:
                await message.answer(result, parse_mode="Markdown")
            except Exception:
                await message.answer(result)
        else:
            await message.answer(t('twin.insights_error', lang))
    except Exception as e:
        logger.error(f"[Twin Insights] Failed: {e}")
        await message.answer(t('twin.insights_error', lang))
