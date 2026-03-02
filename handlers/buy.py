"""
Единая витрина покупок (WP-79).

Команда: /buy (кнопка или команда)
Показывает всё, что можно купить: подписка БР + программы.
Минимум кликов: /buy → кнопка оплаты = 2 клика.
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
from db.queries.aisystant import get_aisystant_id
from clients.aisystant import aisystant
from i18n import t

logger = logging.getLogger(__name__)

buy_router = Router(name="buy")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@buy_router.message(Command("buy"))
async def cmd_buy(message: Message):
    """Команда /buy — витрина покупок."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await message.answer(t('buy.no_account', lang))
        return

    await _show_buy_menu(message, chat_id, aisystant_id, lang)


async def _show_buy_menu(message: Message, chat_id: int, aisystant_id: str, lang: str):
    """Показать витрину: программы + подписка (подписка в конце)."""
    lines = [t('buy.title', lang), ""]
    buttons = []

    # 1. Программы (все в продаже) — сразу с URL-кнопками
    try:
        courses = await aisystant.get_available_courses()
        if courses:
            from handlers.schedule import _create_course_buttons
            paid_courses = []
            for course in courses[:8]:
                code = course.get("code", "")
                name = course.get("courseName", course.get("name", code))
                raw_amount = course.get("price") or course.get("amount") or 0
                try:
                    amount = float(raw_amount)
                except (TypeError, ValueError):
                    amount = 0
                if amount > 0:
                    lines.append(f"  • {name} — {int(amount)} ₽")
                    # Для кнопки убираем префикс программы ("Личное развитие. " и т.п.)
                    dot_pos = name.find(".")
                    btn_name = name[dot_pos + 1:].strip() if dot_pos > 0 else name
                    paid_courses.append((code, btn_name[:25], int(amount)))
            if paid_courses:
                buttons.extend(await _create_course_buttons(
                    aisystant_id, paid_courses, lang, emoji="📚",
                ))
            lines.append("")
    except Exception as e:
        logger.error(f"[Buy] courses error: {e}")

    # 2. Подписка БР — одна кнопка, выбор периода через /subscription
    try:
        is_active = await aisystant.has_active_subscription(aisystant_id)
        if is_active:
            lines.append(t('buy.sub_active', lang))
            buttons.append([InlineKeyboardButton(
                text=t('buy.btn_renew_sub', lang),
                callback_data="aisystant_subscribe",
            )])
        else:
            lines.append(t('buy.sub_section', lang))
            buttons.append([InlineKeyboardButton(
                text=t('buy.btn_buy_sub', lang),
                callback_data="aisystant_subscribe",
            )])
        lines.append("")
    except Exception as e:
        logger.error(f"[Buy] subscription check error: {e}")

    if not buttons:
        lines.append(t('buy.nothing_available', lang))

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
