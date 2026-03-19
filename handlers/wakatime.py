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
    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)
    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    subcommand = parts[1].lower() if len(parts) > 1 else None

    # Проверяем подключение в wakatime_connections (основной flow через /settings)
    from db.queries.wakatime import get_wakatime_connection
    waka_conn = await get_wakatime_connection(telegram_user_id)
    is_connected = waka_conn is not None

    if subcommand == "disconnect":
        if is_connected:
            from db.queries.wakatime import delete_wakatime_connection
            await delete_wakatime_connection(telegram_user_id)
            await message.answer("WakaTime отключён.")
        else:
            await message.answer("WakaTime не подключён.")
        return

    if is_connected:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Статистика",
                        callback_data="wakatime_stats",
                    )
                ],
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
            "Что даёт:\n"
            "• Время работы в IWE учитывается в вашем цифровом двойнике\n"
            "• Данные поступают автоматически — ничего дополнительно делать не нужно\n"
            "• /waka — посмотреть статистику за сегодня и неделю",
            reply_markup=keyboard,
        )
    else:
        await message.answer(
            "WakaTime — трекинг времени работы в IWE.\n\n"
            "Что даёт:\n"
            "• Автоматический учёт времени работы в VS Code\n"
            "• Данные попадают в цифровой двойник для аналитики\n"
            "• Статистика доступна через /waka\n\n"
            "Подключить: /settings → WakaTime → ввести API-ключ с wakatime.com/settings/api-key",
        )


@wakatime_router.callback_query(lambda c: c.data == "wakatime_stats")
async def on_wakatime_stats(callback: CallbackQuery):
    """Показать статистику WakaTime."""
    await callback.message.delete()
    # Переиспользуем существующий /waka handler
    from handlers.commands import cmd_waka
    await cmd_waka(callback.message, state=None)
    await callback.answer()


@wakatime_router.callback_query(lambda c: c.data == "wakatime_disconnect")
async def on_wakatime_disconnect(callback: CallbackQuery):
    """Отключение WakaTime через inline-кнопку."""
    from db.queries.wakatime import delete_wakatime_connection

    telegram_user_id = callback.from_user.id
    await delete_wakatime_connection(telegram_user_id)

    await callback.message.edit_text("WakaTime отключён.")
    await callback.answer()
