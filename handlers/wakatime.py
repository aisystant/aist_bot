"""
Хендлеры интеграции с WakaTime (OAuth, WP-109 Activity Hub).

Команды:
- /wakatime — подключение/статус/отключение
- /wakatime disconnect — отключить
"""

import logging

from aiogram import Router
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

wakatime_router = Router(name="wakatime")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@wakatime_router.message(Command("wakatime"))
async def cmd_wakatime(message: Message):
    """Команда /wakatime — подключение, статус, отключение."""
    from clients.wakatime_oauth import wakatime_oauth

    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)
    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    subcommand = parts[1].lower() if len(parts) > 1 else None

    try:
        is_connected = await wakatime_oauth.is_connected(telegram_user_id)
    except Exception:
        is_connected = False

    if subcommand == "disconnect":
        if is_connected:
            await wakatime_oauth.disconnect(telegram_user_id)
            await message.answer("WakaTime отключён.")
        else:
            await message.answer("WakaTime не подключён.")
        return

    if is_connected:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Отключить WakaTime",
                        callback_data="wakatime_disconnect",
                    )
                ],
            ]
        )
        await message.answer(
            "WakaTime подключён.\n\n"
            "Activity Hub автоматически собирает данные о вашем времени в IDE.",
            reply_markup=keyboard,
        )
    else:
        try:
            auth_url, state = wakatime_oauth.get_authorization_url(telegram_user_id)
        except ValueError as e:
            await message.answer(f"Ошибка конфигурации: {e}")
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подключить WakaTime",
                        url=auth_url,
                    )
                ],
            ]
        )
        await message.answer(
            "Подключите WakaTime для автоматического учёта времени работы в IDE.\n\n"
            "Нажмите кнопку ниже для авторизации:",
            reply_markup=keyboard,
        )


@wakatime_router.callback_query(lambda c: c.data == "wakatime_disconnect")
async def on_wakatime_disconnect(callback: CallbackQuery):
    """Отключение WakaTime через inline-кнопку."""
    from clients.wakatime_oauth import wakatime_oauth

    telegram_user_id = callback.from_user.id
    await wakatime_oauth.disconnect(telegram_user_id)

    await callback.message.edit_text("WakaTime отключён.")
    await callback.answer()
