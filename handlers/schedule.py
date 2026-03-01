"""
Расписание — навигационный хаб (WP-79).

/schedule → хаб с разделами:
- Личное развитие / Рабочее развитие / Семинары / Разбор проекта
- Мастерская Церена (подписка WORKSHOP)
- Подписка БР
- Мои программы

Callbacks:
- sched_cat:{program}          — каталог по программе
- sched_workshop               — мастерская Церена
- aisystant_subscribe          — подписка БР (обработчик в subscription.py)
- schedule_my                  — мои программы
- sched_back                   — возврат в хаб
- schedule_detail:{code}       — детали программы + подтверждение оплаты
- schedule_pay:{code}:{amount} — создание платежа
- sub_pay_ws:{code}:{amount}   — платёж за мастерскую
"""

import logging
from datetime import datetime

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

schedule_router = Router(name="schedule")

SECTION_NAMES = {
    'personal': 'schedule.section_personal',
    'professional': 'schedule.section_professional',
    'seminars': 'schedule.section_seminars',
    'reviews': 'schedule.section_reviews',
}

# Hub menu sections: key, callback_data, emoji, i18n label
MENU_SECTIONS = [
    ('personal',     'sched_cat:personal',     '📚', 'schedule.menu_personal'),
    ('professional', 'sched_cat:professional', '💼', 'schedule.menu_professional'),
    ('seminars',     'sched_cat:seminars',     '🎤', 'schedule.menu_seminars'),
    ('reviews',      'sched_cat:reviews',      '🔍', 'schedule.menu_reviews'),
    ('workshop',     'sched_workshop',         '🔧', 'schedule.menu_workshop'),
    ('subscription', 'aisystant_subscribe',    '💎', 'schedule.menu_subscription'),
    ('my_courses',   'schedule_my',            '📋', 'schedule.menu_my_courses'),
]


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


def _format_datetime(dt_str: str, lang: str) -> str:
    """Format ISO datetime to user-friendly string."""
    try:
        dt = datetime.fromisoformat(dt_str)
        if lang == 'en':
            return dt.strftime("%b %d, %H:%M")
        return dt.strftime("%d.%m %H:%M")
    except (ValueError, TypeError):
        return dt_str or "—"


def _format_date(date_str: str, lang: str) -> str:
    """Format date string to user-friendly format."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if lang == 'en':
            return dt.strftime("%b %d, %Y")
        return dt.strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return date_str or "—"


# ── Hub ─────────────────────────────────────────────────

async def _show_hub(message: Message, chat_id: int):
    """Показать навигационный хаб расписания."""
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    lines = [t('schedule.hub_title', lang), ""]

    # Ближайшие занятия (для привязанных пользователей)
    aisystant_id = await get_aisystant_id(chat_id)
    if aisystant_id:
        try:
            lessons = await aisystant.get_user_lessons(aisystant_id)
            if lessons:
                lines.append(t('schedule.hub_upcoming', lang))
                for lesson in lessons[:3]:
                    potok = lesson.get("potok", {})
                    course_name = potok.get("courseName", potok.get("code", "—"))
                    lesson_data = lesson.get("lesson", {})
                    lesson_dt = _format_datetime(lesson_data.get("datetime", ""), lang)
                    lines.append(t('schedule.hub_upcoming_item', lang,
                                    course=course_name, datetime=lesson_dt))
                lines.append("")
        except Exception as e:
            logger.error(f"[Schedule] hub lessons error: {e}")

    lines.append(t('schedule.hub_choose', lang))

    # Кнопки разделов (2 в ряд)
    buttons = []
    row = []
    for _key, callback_data, emoji, i18n_key in MENU_SECTIONS:
        label = t(i18n_key, lang)
        row.append(InlineKeyboardButton(
            text=f"{emoji} {label}",
            callback_data=callback_data,
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


@schedule_router.message(Command("schedule"))
async def cmd_schedule(message: Message):
    """Команда /schedule — навигационный хаб."""
    await _show_hub(message, message.chat.id)


@schedule_router.callback_query(F.data == "sched_back")
async def callback_back(callback: CallbackQuery):
    """Возврат в хаб."""
    await callback.answer()
    await _show_hub(callback.message, callback.from_user.id)


@schedule_router.callback_query(F.data == "schedule_courses")
async def callback_courses_legacy(callback: CallbackQuery):
    """Legacy stub: старая кнопка → хаб."""
    await callback.answer()
    await _show_hub(callback.message, callback.from_user.id)


# ── Каталог по программе ────────────────────────────────

@schedule_router.callback_query(F.data.startswith("sched_cat:"))
async def callback_category(callback: CallbackQuery):
    """Потоки одной программы с кнопками оплаты."""
    category = callback.data.split(":", 1)[1]
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    try:
        courses = await aisystant.get_available_courses()
    except Exception as e:
        logger.error(f"[Schedule] get_available_courses error: {e}")
        await callback.message.answer(t('schedule.error', lang))
        return

    filtered = [c for c in courses if c.get("program") == category]

    if not filtered:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('schedule.btn_back', lang), callback_data="sched_back")],
        ])
        await callback.message.answer(t('schedule.category_empty', lang), reply_markup=keyboard)
        return

    section_name = t(SECTION_NAMES.get(category, 'schedule.section_personal'), lang)
    lines = [f"*{section_name}*", ""]

    buttons = []
    aisystant_id = await get_aisystant_id(chat_id)

    for course in filtered:
        name = course.get("courseName", course.get("code", "—"))
        start = _format_date(course.get("started", ""), lang)
        price = course.get("price")
        price_str = f"{int(price)} ₽" if price else "бесплатно"
        lines.append(t('schedule.course_item', lang, name=name, start=start, price=price_str))

        # Кнопка оплаты (только для привязанных)
        if aisystant_id and price:
            code = course.get("code", "")
            short_name = name[:25]
            buttons.append([InlineKeyboardButton(
                text=f"💳 {short_name} — {price_str}",
                callback_data=f"schedule_detail:{code}",
            )])

    # Кнопка «Назад»
    buttons.append([InlineKeyboardButton(
        text=t('schedule.btn_back', lang), callback_data="sched_back",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


# ── Мастерская Церена ───────────────────────────────────

@schedule_router.callback_query(F.data == "sched_workshop")
async def callback_workshop(callback: CallbackQuery):
    """Мастерская Церена — подписка WORKSHOP."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('schedule.no_account', lang))
        return

    lines = [t('schedule.workshop_text', lang), ""]
    buttons = []

    try:
        # Проверяем активную подписку WORKSHOP
        ws_sub = await aisystant.get_subscription_status_by_purpose(aisystant_id, "WORKSHOP")
        if ws_sub:
            lines.append(t('schedule.workshop_active', lang))
            buttons.append([InlineKeyboardButton(
                text=t('schedule.btn_workshop_renew', lang),
                callback_data="sched_ws_tariffs",
            )])
        else:
            # Показываем тарифы сразу
            tariffs = await aisystant.get_subscription_tariffs(aisystant_id, purpose="WORKSHOP")
            if tariffs:
                from handlers.subscription import _parse_tariff, _period_label
                paid_tariffs = []
                for tariff in tariffs[:5]:
                    code, name, amount, periodicity = _parse_tariff(tariff)
                    period = _period_label(periodicity, lang)
                    lines.append(f"  • {period} — {amount} ₽")
                    if amount > 0:
                        paid_tariffs.append((code, amount, period))
                # Один тариф → сразу создаём платёж
                if len(paid_tariffs) == 1:
                    code, amount, period = paid_tariffs[0]
                    try:
                        result = await aisystant.create_subscription_payment(
                            aisystant_id, code, amount, purpose="WORKSHOP",
                        )
                        if result and result.get("confirmationUrl"):
                            url = result["confirmationUrl"]
                            buttons.append([InlineKeyboardButton(
                                text=t('aisystant_sub.btn_pay_link', lang), url=url,
                            )])
                        else:
                            buttons.append([InlineKeyboardButton(
                                text=f"💳 {period} — {amount} ₽",
                                callback_data=f"sub_pay_ws:{code}:{amount}",
                            )])
                    except Exception as e:
                        logger.error(f"[Schedule] workshop auto-payment error: {e}")
                        buttons.append([InlineKeyboardButton(
                            text=f"💳 {period} — {amount} ₽",
                            callback_data=f"sub_pay_ws:{code}:{amount}",
                        )])
                else:
                    for code, amount, period in paid_tariffs:
                        buttons.append([InlineKeyboardButton(
                            text=f"💳 {period} — {amount} ₽",
                            callback_data=f"sub_pay_ws:{code}:{amount}",
                        )])
    except Exception as e:
        logger.error(f"[Schedule] workshop error: {e}")

    buttons.append([InlineKeyboardButton(
        text=t('schedule.btn_back', lang), callback_data="sched_back",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


@schedule_router.callback_query(F.data == "sched_ws_tariffs")
async def callback_ws_tariffs(callback: CallbackQuery):
    """Тарифы мастерской для продления."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('schedule.no_account', lang))
        return

    try:
        tariffs = await aisystant.get_subscription_tariffs(aisystant_id, purpose="WORKSHOP")
    except Exception as e:
        logger.error(f"[Schedule] ws tariffs error: {e}")
        await callback.message.answer(t('schedule.error', lang))
        return

    if not tariffs:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('schedule.btn_back', lang), callback_data="sched_back")],
        ])
        await callback.message.answer(t('schedule.category_empty', lang), reply_markup=keyboard)
        return

    from handlers.subscription import _parse_tariff, _period_label

    lines = [t('schedule.workshop_text', lang), ""]
    buttons = []
    paid_tariffs = []
    for tariff in tariffs[:5]:
        code, name, amount, periodicity = _parse_tariff(tariff)
        period = _period_label(periodicity, lang)
        lines.append(f"  • {period} — {amount} ₽")
        if amount > 0:
            paid_tariffs.append((code, amount, period))

    # Один тариф → сразу создаём платёж
    if len(paid_tariffs) == 1:
        code, amount, period = paid_tariffs[0]
        try:
            result = await aisystant.create_subscription_payment(
                aisystant_id, code, amount, purpose="WORKSHOP",
            )
            if result and result.get("confirmationUrl"):
                url = result["confirmationUrl"]
                buttons.append([InlineKeyboardButton(
                    text=t('aisystant_sub.btn_pay_link', lang), url=url,
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"💳 {period} — {amount} ₽",
                    callback_data=f"sub_pay_ws:{code}:{amount}",
                )])
        except Exception as e:
            logger.error(f"[Schedule] ws renewal auto-payment error: {e}")
            buttons.append([InlineKeyboardButton(
                text=f"💳 {period} — {amount} ₽",
                callback_data=f"sub_pay_ws:{code}:{amount}",
            )])
    else:
        for code, amount, period in paid_tariffs:
            buttons.append([InlineKeyboardButton(
                text=f"💳 {period} — {amount} ₽",
                callback_data=f"sub_pay_ws:{code}:{amount}",
            )])

    buttons.append([InlineKeyboardButton(
        text=t('schedule.btn_back', lang), callback_data="sched_back",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


# ── Мои программы ──────────────────────────────────────

@schedule_router.callback_query(F.data == "schedule_my")
async def callback_my_courses(callback: CallbackQuery):
    """Мои программы."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('schedule.no_account', lang))
        return

    try:
        courses = await aisystant.get_user_courses(aisystant_id)
    except Exception as e:
        logger.error(f"[Schedule] get_user_courses error: {e}")
        await callback.message.answer(t('schedule.error', lang))
        return

    if not courses:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('schedule.btn_back', lang), callback_data="sched_back")],
        ])
        await callback.message.answer(t('schedule.my_courses_empty', lang), reply_markup=keyboard)
        return

    lines = [t('schedule.my_courses_title', lang), ""]
    for passing in courses[:15]:
        potok = passing.get("potok", {})
        name = potok.get("courseName", potok.get("code", "—"))
        status = potok.get("status", "—")
        lines.append(t('schedule.my_course_item', lang, name=name, status=status))

    buttons = [
        [InlineKeyboardButton(text=t('schedule.btn_back', lang), callback_data="sched_back")],
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


# ── Детали программы + оплата ──────────────────────────

@schedule_router.callback_query(F.data.startswith("schedule_detail:"))
async def callback_course_detail(callback: CallbackQuery):
    """Детали программы + кнопка покупки."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)
    code = callback.data.split(":", 1)[1]

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('schedule.no_account', lang))
        return

    # Получаем доступные интернатуры с ценами
    try:
        internships = await aisystant.get_available_internships(aisystant_id)
    except Exception as e:
        logger.error(f"[Schedule] get_available_internships error: {e}")
        await callback.message.answer(t('schedule.error', lang))
        return

    try:
        course = next((i for i in internships if i.get("code") == code), None)
        if not course:
            all_courses = await aisystant.get_available_courses()
            course = next((c for c in all_courses if c.get("code") == code), None)

        if not course:
            await callback.message.answer(t('schedule.catalog_empty', lang))
            return

        name = course.get("courseName", course.get("name", code))
        raw_amount = course.get("amount") or course.get("price") or 0
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            amount = 0

        if amount > 0:
            text = t('schedule.payment_confirm', lang, course=name, amount=int(amount))
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('schedule.btn_pay', lang, amount=int(amount)),
                    callback_data=f"schedule_pay:{code}:{int(amount)}",
                )],
                [InlineKeyboardButton(
                    text=t('schedule.btn_cancel', lang),
                    callback_data="sched_back",
                )],
            ])
            await callback.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await callback.message.answer(f"*{name}*\n\nБесплатная программа.", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[Schedule] course_detail error for code={code}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.message.answer(t('schedule.error', lang))


@schedule_router.callback_query(F.data.startswith("schedule_pay:"))
async def callback_pay(callback: CallbackQuery):
    """Создать платёж за программу."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    parts = callback.data.split(":")
    code = parts[1]
    amount = float(parts[2])

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('schedule.no_account', lang))
        return

    try:
        result = await aisystant.create_internship_payment(aisystant_id, code, amount)
    except Exception as e:
        logger.error(f"[Schedule] create_internship_payment error: {e}")
        await callback.message.answer(t('schedule.payment_error', lang))
        return

    if not result or not result.get("confirmationUrl"):
        await callback.message.answer(t('schedule.payment_error', lang))
        return

    url = result["confirmationUrl"]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('schedule.btn_pay_link', lang), url=url)],
    ])
    await callback.message.answer(t('schedule.payment_success', lang), reply_markup=keyboard)
