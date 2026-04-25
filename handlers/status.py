"""
/status — статус платформы Aisystant.

Показывает ссылку на public status page и канал инцидентов.
Реализация WP-244 Ф7 (User-Facing Platform Health, DP.SC.124).

Источник истины — Better Stack (https://aisystant.betteruptime.com).
Канал инцидентов — @aisystant_status (Cloudflare Worker observability-webhook постит сюда).
"""

import logging

from aiogram import Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

logger = logging.getLogger(__name__)

status_router = Router(name="status")


@status_router.message(Command("status"))
async def cmd_status(message: Message):
    """Показывает статус платформы — ссылку на dashboard и канал инцидентов."""
    text = (
        "🟢 <b>Статус платформы Aisystant</b>\n"
        "\n"
        "📊 <b>Публичный dashboard:</b>\n"
        "<a href=\"https://aisystant.betteruptime.com\">aisystant.betteruptime.com</a>\n"
        "Real-time статус сервисов, composite uptime «по девяткам», история инцидентов за 90 дней.\n"
        "\n"
        "📢 <b>Канал инцидентов:</b>\n"
        "<a href=\"https://t.me/aisystant_status\">@aisystant_status</a>\n"
        "Подпишись, чтобы автоматически получать уведомления о проблемах в работе платформы и об их восстановлении.\n"
        "\n"
        "<i>Реализация: Better Stack monitoring + Cloudflare Worker observability-webhook (WP-244).</i>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📊 Открыть dashboard",
            url="https://aisystant.betteruptime.com",
        )],
        [InlineKeyboardButton(
            text="📢 Подписаться на канал",
            url="https://t.me/aisystant_status",
        )],
    ])

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
