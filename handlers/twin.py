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


def _profile_text(profile: dict, lang: str) -> str:
    """Формирует текст профиля Digital Twin."""
    degree = profile.get("degree", t('twin.not_set', lang))
    stage = profile.get("stage", t('twin.not_set', lang))
    indicators = profile.get("indicators", {})
    pref = indicators.get("IND.1.PREF", {}) if isinstance(indicators, dict) else {}
    objective = pref.get("objective", t('twin.not_set', lang)) if isinstance(pref, dict) else t('twin.not_set', lang)
    roles = pref.get("role_set", []) if isinstance(pref, dict) else []
    time_budget = pref.get("weekly_time_budget", t('twin.not_set_m', lang)) if isinstance(pref, dict) else t('twin.not_set_m', lang)
    roles_text = ", ".join(roles) if isinstance(roles, list) and roles else t('twin.not_set_plural', lang)

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
                    await conn.execute('UPDATE interns SET dt_connected_at = NULL WHERE chat_id = $1', telegram_user_id)
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
            lines = [f"*{t('twin.degrees_title', lang)}*\n"]
            for d in degrees:
                name = d.get("name", d.get("code", "?"))
                desc = d.get("description", "")
                lines.append(f"- *{name}*{f' — {desc}' if desc else ''}")
            await message.answer("\n".join(lines), parse_mode="Markdown")
        else:
            await message.answer(t('twin.degrees_error', lang))
        return

    # По умолчанию: показать профиль
    await message.answer(t('twin.loading_profile', lang))
    profile = await digital_twin.get_user_profile(telegram_user_id)

    if profile is None:
        await message.answer(t('twin.unavailable', lang))
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('twin.btn_update_profile', lang), callback_data="twin_profile")],
        [InlineKeyboardButton(text=t('twin.btn_degrees', lang), callback_data="twin_degrees")],
        [InlineKeyboardButton(text=t('twin.btn_disconnect', lang), callback_data="twin_disconnect")],
    ])

    await message.answer(_profile_text(profile, lang), parse_mode="Markdown", reply_markup=keyboard)


@twin_router.callback_query(F.data == "twin_profile")
async def callback_twin_profile(callback: CallbackQuery):
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not digital_twin.is_connected(telegram_user_id):
        await callback.answer(t('twin.not_connected_alert', lang), show_alert=True)
        return

    await callback.answer()

    profile = await digital_twin.get_user_profile(telegram_user_id)
    if profile is None:
        await callback.message.answer(t('twin.unavailable_short', lang))
        return

    await callback.message.answer(_profile_text(profile, lang), parse_mode="Markdown")


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
        lines = [f"*{t('twin.degrees_title', lang)}*\n"]
        for d in degrees:
            name = d.get("name", d.get("code", "?"))
            desc = d.get("description", "")
            lines.append(f"- *{name}*{f' — {desc}' if desc else ''}")
        await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    else:
        await callback.message.answer(t('twin.degrees_error', lang))


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
            await conn.execute('UPDATE interns SET dt_connected_at = NULL WHERE chat_id = $1', telegram_user_id)
    except Exception:
        pass
    await callback.answer(t('twin.disconnected_alert', lang), show_alert=True)
    await callback.message.edit_text(
        t('twin.disconnected_desc', lang),
        parse_mode="Markdown"
    )
