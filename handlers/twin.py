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

logger = logging.getLogger(__name__)

twin_router = Router(name="twin")


@twin_router.message(Command("twin"))
async def cmd_twin(message: Message):
    """Команда для работы с Digital Twin.

    Подкоманды:
    - /twin — показать профиль или предложить подключение
    - /twin disconnect — отключить интеграцию
    - /twin objective <текст> — установить цель обучения
    - /twin roles — показать роли
    - /twin degrees — показать все степени
    """
    from clients.digital_twin import digital_twin

    telegram_user_id = message.chat.id

    text = message.text or ""
    parts = text.strip().split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) > 1 else None
    arg = parts[2] if len(parts) > 2 else None

    is_connected = digital_twin.is_connected(telegram_user_id)

    if subcommand == "disconnect":
        if is_connected:
            digital_twin.disconnect(telegram_user_id)
            await message.answer("Digital Twin отключён.")
        else:
            await message.answer("Digital Twin не был подключён.")
        return

    if not is_connected:
        auth_url, state = digital_twin.get_authorization_url(telegram_user_id)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подключить Digital Twin", url=auth_url)]
        ])
        await message.answer(
            "*Подключение к Digital Twin*\n\n"
            "Нажмите кнопку ниже, чтобы авторизоваться.\n"
            "После авторизации вы сможете просматривать и редактировать свой профиль.",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    if subcommand == "objective" and arg:
        await message.answer("Сохраняю цель обучения...")
        result = await digital_twin.set_learning_objective(telegram_user_id, arg)
        if result:
            await message.answer(f"Цель обучения обновлена:\n*{arg}*", parse_mode="Markdown")
        else:
            await message.answer("Не удалось сохранить цель. Digital Twin недоступен.")
        return

    if subcommand == "roles":
        roles = await digital_twin.get_roles(telegram_user_id)
        if roles:
            roles_text = ", ".join(roles) if isinstance(roles, list) else str(roles)
            await message.answer(f"*Ваши роли:*\n{roles_text}", parse_mode="Markdown")
        else:
            await message.answer("Роли не заданы или Digital Twin недоступен.")
        return

    if subcommand == "degrees":
        degrees = await digital_twin.get_degrees(telegram_user_id)
        if degrees:
            lines = ["*Степени квалификации:*\n"]
            for d in degrees:
                name = d.get("name", d.get("code", "?"))
                desc = d.get("description", "")
                lines.append(f"- *{name}*{f' — {desc}' if desc else ''}")
            await message.answer("\n".join(lines), parse_mode="Markdown")
        else:
            await message.answer("Не удалось получить степени. Digital Twin недоступен.")
        return

    # По умолчанию: показать профиль
    await message.answer("Загружаю профиль из Digital Twin...")
    profile = await digital_twin.get_user_profile(telegram_user_id)

    if profile is None:
        await message.answer(
            "Digital Twin недоступен или профиль не найден.\n\n"
            "Попробуйте переподключиться: /twin disconnect, затем /twin"
        )
        return

    degree = profile.get("degree", "не задана")
    stage = profile.get("stage", "не задана")
    indicators = profile.get("indicators", {})
    pref = indicators.get("IND.1.PREF", {}) if isinstance(indicators, dict) else {}
    objective = pref.get("objective", "не задана") if isinstance(pref, dict) else "не задана"
    roles = pref.get("role_set", []) if isinstance(pref, dict) else []
    time_budget = pref.get("weekly_time_budget", "не задан") if isinstance(pref, dict) else "не задан"
    roles_text = ", ".join(roles) if isinstance(roles, list) and roles else "не заданы"

    text_msg = (
        f"*Ваш Digital Twin*\n\n"
        f"*Степень:* {degree}\n"
        f"*Ступень:* {stage}\n"
        f"*Цель обучения:* {objective}\n"
        f"*Роли:* {roles_text}\n"
        f"*Бюджет времени:* {time_budget} ч/нед"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить профиль", callback_data="twin_profile")],
        [InlineKeyboardButton(text="Степени", callback_data="twin_degrees")],
        [InlineKeyboardButton(text="Отключить", callback_data="twin_disconnect")],
    ])

    await message.answer(text_msg, parse_mode="Markdown", reply_markup=keyboard)


@twin_router.callback_query(F.data == "twin_profile")
async def callback_twin_profile(callback: CallbackQuery):
    """Callback для обновления профиля Digital Twin."""
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id

    if not digital_twin.is_connected(telegram_user_id):
        await callback.answer("Digital Twin не подключён", show_alert=True)
        return

    await callback.answer()

    profile = await digital_twin.get_user_profile(telegram_user_id)
    if profile is None:
        await callback.message.answer("Digital Twin недоступен. Попробуйте позже.")
        return

    degree = profile.get("degree", "не задана")
    stage = profile.get("stage", "не задана")
    indicators = profile.get("indicators", {})
    pref = indicators.get("IND.1.PREF", {}) if isinstance(indicators, dict) else {}
    objective = pref.get("objective", "не задана") if isinstance(pref, dict) else "не задана"
    roles = pref.get("role_set", []) if isinstance(pref, dict) else []
    time_budget = pref.get("weekly_time_budget", "не задан") if isinstance(pref, dict) else "не задан"
    roles_text = ", ".join(roles) if isinstance(roles, list) and roles else "не заданы"

    text_msg = (
        f"*Ваш Digital Twin*\n\n"
        f"*Степень:* {degree}\n"
        f"*Ступень:* {stage}\n"
        f"*Цель обучения:* {objective}\n"
        f"*Роли:* {roles_text}\n"
        f"*Бюджет времени:* {time_budget} ч/нед"
    )

    await callback.message.answer(text_msg, parse_mode="Markdown")


@twin_router.callback_query(F.data == "twin_degrees")
async def callback_twin_degrees(callback: CallbackQuery):
    """Callback для показа степеней."""
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id

    if not digital_twin.is_connected(telegram_user_id):
        await callback.answer("Digital Twin не подключён", show_alert=True)
        return

    await callback.answer()

    degrees = await digital_twin.get_degrees(telegram_user_id)
    if degrees:
        lines = ["*Степени квалификации:*\n"]
        for d in degrees:
            name = d.get("name", d.get("code", "?"))
            desc = d.get("description", "")
            lines.append(f"- *{name}*{f' — {desc}' if desc else ''}")
        await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    else:
        await callback.message.answer("Не удалось получить степени. Digital Twin недоступен.")


@twin_router.callback_query(F.data == "twin_disconnect")
async def callback_twin_disconnect(callback: CallbackQuery):
    """Callback для отключения Digital Twin."""
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id

    if not digital_twin.is_connected(telegram_user_id):
        await callback.answer("Digital Twin уже отключён", show_alert=True)
        return

    digital_twin.disconnect(telegram_user_id)
    await callback.answer("Digital Twin отключён", show_alert=True)
    await callback.message.edit_text(
        "*Digital Twin отключён*\n\nИспользуйте /twin чтобы подключиться снова.",
        parse_mode="Markdown"
    )
