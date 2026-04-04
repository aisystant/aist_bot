"""
Регистрация через Ory — привязка telegram_id → ory_id (WP-187).

Callback: ory_register — отправляет ссылку на регистрацию в Ory.
Используется в: settings (T0), paywall (T0).
"""

import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from clients.ory_oauth import ory_oauth
from config import ORY_CLIENT_ID, get_logger

logger = get_logger(__name__)

ory_register_router = Router(name="ory_register")


@ory_register_router.callback_query(F.data == "ory_register")
async def on_ory_register(callback: CallbackQuery):
    """Отправляет ссылку для регистрации/входа через Ory."""
    if not ORY_CLIENT_ID:
        await callback.answer("Регистрация временно недоступна", show_alert=True)
        return

    telegram_user_id = callback.from_user.id
    auth_url, _ = ory_oauth.get_authorization_url(telegram_user_id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Зарегистрироваться", url=auth_url)],
    ])

    await callback.message.answer(
        "Для доступа к расширенным функциям платформы "
        "зарегистрируйтесь или войдите через Aisystant:",
        reply_markup=keyboard,
    )
    await callback.answer()
