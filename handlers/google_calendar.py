"""
Хендлеры интеграции с Google Calendar (OAuth, просмотр событий).

Команды:
- /calendar — подключение/статус/просмотр событий
- /calendar disconnect — отключить
"""

import logging

from aiogram import Router, F
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

gcal_router = Router(name="google_calendar")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@gcal_router.message(Command("calendar"))
async def cmd_calendar(message: Message):
    """Команда /calendar — подключение, статус, просмотр событий."""
    from clients.google_calendar_oauth import google_calendar_oauth

    telegram_user_id = message.chat.id
    text = message.text or ""
    parts = text.strip().split(maxsplit=1)

    # /calendar disconnect
    if len(parts) > 1 and parts[1].strip().lower() == "disconnect":
        if await google_calendar_oauth.is_connected(telegram_user_id):
            await google_calendar_oauth.disconnect(telegram_user_id)
            await message.answer("Google Calendar отключён.")
        else:
            await message.answer("Google Calendar не подключён.")
        return

    # Проверяем подключение
    if await google_calendar_oauth.is_connected(telegram_user_id):
        await _show_today_events(message, telegram_user_id)
    else:
        await _show_connect(message, telegram_user_id)


@gcal_router.callback_query(F.data == "gcal_today")
async def cb_gcal_today(callback: CallbackQuery):
    """Callback: показать события на сегодня."""
    from clients.google_calendar_oauth import google_calendar_oauth, format_events_message

    telegram_user_id = callback.from_user.id
    events = await google_calendar_oauth.get_today_events(telegram_user_id)

    if events is None:
        await callback.answer("Не удалось получить события. Попробуйте /calendar", show_alert=True)
        return

    text = format_events_message(events, title="Сегодня")
    keyboard = _kb_from_today()

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@gcal_router.callback_query(F.data == "gcal_tomorrow")
async def cb_gcal_tomorrow(callback: CallbackQuery):
    """Callback: показать события на завтра."""
    from clients.google_calendar_oauth import google_calendar_oauth, format_events_message

    telegram_user_id = callback.from_user.id
    events = await google_calendar_oauth.get_tomorrow_events(telegram_user_id)

    if events is None:
        await callback.answer("Не удалось получить события. Попробуйте /calendar", show_alert=True)
        return

    text = format_events_message(events, title="Завтра")
    keyboard = _kb_from_tomorrow()

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@gcal_router.callback_query(F.data == "gcal_week")
async def cb_gcal_week(callback: CallbackQuery):
    """Callback: показать события на 7 дней."""
    from clients.google_calendar_oauth import google_calendar_oauth, format_week_events_message

    telegram_user_id = callback.from_user.id
    events = await google_calendar_oauth.get_week_events(telegram_user_id)

    if events is None:
        await callback.answer("Не удалось получить события. Попробуйте /calendar", show_alert=True)
        return

    text = format_week_events_message(events)
    keyboard = _kb_from_week()

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@gcal_router.callback_query(F.data == "gcal_disconnect")
async def cb_gcal_disconnect(callback: CallbackQuery):
    """Callback: отключить Google Calendar."""
    from clients.google_calendar_oauth import google_calendar_oauth

    telegram_user_id = callback.from_user.id
    await google_calendar_oauth.disconnect(telegram_user_id)

    try:
        await callback.message.edit_text("Google Calendar отключён.\n\nДля повторного подключения: /calendar")
    except Exception:
        await callback.message.answer("Google Calendar отключён.\n\nДля повторного подключения: /calendar")
    await callback.answer()


async def _show_connect(message: Message, telegram_user_id: int):
    """Показывает кнопку подключения."""
    from clients.google_calendar_oauth import google_calendar_oauth

    try:
        auth_url, state = google_calendar_oauth.get_authorization_url(telegram_user_id)
    except ValueError as e:
        await message.answer(f"Google Calendar не настроен: {e}")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подключить Google Calendar", url=auth_url)]
        ]
    )

    await message.answer(
        "Нажмите кнопку, чтобы подключить Google Calendar.\n"
        "Бот получит доступ только для чтения ваших событий.",
        reply_markup=keyboard,
    )


async def _show_today_events(message: Message, telegram_user_id: int):
    """Показывает события на сегодня."""
    from clients.google_calendar_oauth import google_calendar_oauth, format_events_message
    from helpers.typing_indicator import keep_typing

    async with keep_typing(message):
        events = await google_calendar_oauth.get_today_events(telegram_user_id)

    if events is None:
        await message.answer(
            "Не удалось получить события.\n"
            "Попробуйте переподключиться: /calendar disconnect, затем /calendar"
        )
        return

    text = format_events_message(events, title="Сегодня")
    keyboard = _kb_from_today()
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


# --- Контекстные клавиатуры ---

def _kb_from_today() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Завтра", callback_data="gcal_tomorrow"),
                InlineKeyboardButton(text="7 дней", callback_data="gcal_week"),
            ],
            [InlineKeyboardButton(text="Отключить", callback_data="gcal_disconnect")],
        ]
    )


def _kb_from_tomorrow() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="gcal_today"),
                InlineKeyboardButton(text="7 дней", callback_data="gcal_week"),
            ],
            [InlineKeyboardButton(text="Отключить", callback_data="gcal_disconnect")],
        ]
    )


def _kb_from_week() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="gcal_today"),
                InlineKeyboardButton(text="Завтра", callback_data="gcal_tomorrow"),
            ],
            [InlineKeyboardButton(text="Отключить", callback_data="gcal_disconnect")],
        ]
    )
