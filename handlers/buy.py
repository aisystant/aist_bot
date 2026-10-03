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

from core.tier_detector import has_active_subscription
from db.queries import get_intern
from db.queries.aisystant import get_aisystant_id
from clients.aisystant import aisystant, parse_amount
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
    """Показать витрину: витрина семинаров + подписка первыми, программы ниже."""
    lines = [t('buy.title', lang), ""]
    buttons = []

    # 1. Витрина семинаров + Подписка БР — в одном ряду
    try:
        is_active = await has_active_subscription(chat_id, aisystant_id)
        if is_active:
            lines.append(t('buy.sub_active', lang))
            sub_btn = InlineKeyboardButton(
                text=t('buy.btn_renew_sub', lang),
                callback_data="aisystant_subscribe",
            )
        else:
            lines.append(t('buy.sub_section', lang))
            sub_btn = InlineKeyboardButton(
                text=t('buy.btn_buy_sub', lang),
                callback_data="aisystant_subscribe",
            )
        lines.append("")
    except Exception as e:
        logger.error(f"[Buy] subscription check error: {e}")
        sub_btn = InlineKeyboardButton(
            text="💎 " + t('schedule.menu_subscription', lang),
            callback_data="aisystant_subscribe",
        )

    buttons.append([
        InlineKeyboardButton(
            text="🎬 " + t('schedule.menu_showcase', lang),
            callback_data="showcase_main",
        ),
        sub_btn,
    ])

    # 3. Программы (доступные лично пользователю, включая просроченные по дате
    #    старта серии/семинары — WP-5, расхождение с @SystemsSchool_bot) — ниже
    # Пилот попросил закрепить семинар «Интеллектуальная рабочая среда» вторым
    # пунктом (сразу под витриной семинаров/подпиской), не полагаясь на порядок API.
    PINNED_NAME_SUBSTRING = "интеллектуальная рабочая среда"
    try:
        courses = await aisystant.get_available_internships(aisystant_id)
        if courses:
            from handlers.schedule import _create_course_buttons, _format_date
            entries = []
            for course in courses:
                if course.get("nextPaymentIndex") is not None:
                    continue  # открытая рассрочка — не новая покупка
                code = course.get("code", "")
                name = course.get("courseName", course.get("name", code))
                raw_amount = course.get("price") or course.get("amount") or 0
                amount = parse_amount(raw_amount)
                if amount > 0:
                    entries.append((code, name, amount, course.get("started", "")))
            entries.sort(key=lambda e: PINNED_NAME_SUBSTRING not in e[1].lower())

            paid_courses = []
            for code, name, amount, started in entries:
                start = _format_date(started, lang)
                price = f"{int(amount):,}".replace(",", " ")
                lines.append(f"• *{name}*\n  Старт: {start}. {price} ₽")
                btn_name = name.strip()
                if len(btn_name) > 30:
                    btn_name = btn_name[:27] + "..."
                paid_courses.append((code, btn_name, int(amount)))
            if paid_courses:
                buttons.extend(await _create_course_buttons(
                    aisystant_id, paid_courses, lang, chat_id, emoji="📚",
                ))
            lines.append("")
    except Exception as e:
        logger.error(f"[Buy] courses error: {e}")

    if not buttons:
        lines.append(t('buy.nothing_available', lang))
    else:
        lines.append(t('buy.payment_note', lang))

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
