"""
Расписание, каталог курсов, оплата (WP-79).

Команды:
- /schedule — расписание занятий + навигация к каталогу
- callback schedule_courses — каталог доступных курсов
- callback schedule_my — мои курсы
- callback schedule_buy:{code}:{amount} — покупка курса

Доступно всем тирам (кнопка «📋 Расписание»):
- T1 new: только каталог (без личного расписания)
- T1+ (с aisystant_id): личное расписание + каталог + мои курсы
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


@schedule_router.message(Command("schedule"))
async def cmd_schedule(message: Message):
    """Команда /schedule — расписание занятий."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    aisystant_id = await get_aisystant_id(chat_id)

    # Нет привязки — показываем каталог и предлагаем привязать
    if not aisystant_id:
        courses = await aisystant.get_available_courses()
        if courses:
            text = _build_catalog_text(courses, lang)
            await message.answer(text, parse_mode="Markdown")
        else:
            await message.answer(t('schedule.no_account', lang))
        return

    # Есть привязка — показываем занятия
    try:
        lessons = await aisystant.get_user_lessons(aisystant_id)
    except Exception as e:
        logger.error(f"[Schedule] get_user_lessons error: {e}")
        await message.answer(t('schedule.error', lang))
        return

    buttons = [
        [InlineKeyboardButton(text=t('schedule.btn_courses', lang), callback_data="schedule_courses")],
        [InlineKeyboardButton(text=t('schedule.btn_my_courses', lang), callback_data="schedule_my")],
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    if not lessons:
        await message.answer(t('schedule.no_lessons', lang), reply_markup=keyboard)
        return

    lines = [t('schedule.title', lang), ""]
    for lesson in lessons[:10]:
        potok = lesson.get("potok", {})
        course_name = potok.get("courseName", potok.get("code", "—"))
        lesson_data = lesson.get("lesson", {})
        lesson_dt = _format_datetime(lesson_data.get("datetime", ""), lang)
        location = lesson_data.get("location", "Онлайн")
        lines.append(t('schedule.lesson_item', lang,
                        course=course_name, datetime=lesson_dt, location=location))
        lines.append("")

    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


@schedule_router.callback_query(F.data == "schedule_courses")
async def callback_courses(callback: CallbackQuery):
    """Каталог доступных курсов."""
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

    if not courses:
        await callback.message.answer(t('schedule.catalog_empty', lang))
        return

    text = _build_catalog_text(courses, lang)

    # Inline-кнопки для покупки (для привязанных пользователей)
    aisystant_id = await get_aisystant_id(chat_id)
    buttons = []
    if aisystant_id:
        for course in courses[:8]:
            code = course.get("code", "")
            name = course.get("courseName", code)[:30]
            # Используем internships для получения цены
            buttons.append([InlineKeyboardButton(
                text=f"💳 {name}",
                callback_data=f"schedule_detail:{code}",
            )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


@schedule_router.callback_query(F.data == "schedule_my")
async def callback_my_courses(callback: CallbackQuery):
    """Мои курсы."""
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
            [InlineKeyboardButton(text=t('schedule.btn_courses', lang), callback_data="schedule_courses")],
        ])
        await callback.message.answer(t('schedule.my_courses_empty', lang), reply_markup=keyboard)
        return

    lines = [t('schedule.my_courses_title', lang), ""]
    for passing in courses[:15]:
        potok = passing.get("potok", {})
        name = potok.get("courseName", potok.get("code", "—"))
        status = potok.get("status", "—")
        lines.append(t('schedule.my_course_item', lang, name=name, status=status))

    await callback.message.answer("\n".join(lines), parse_mode="Markdown")


@schedule_router.callback_query(F.data.startswith("schedule_detail:"))
async def callback_course_detail(callback: CallbackQuery):
    """Детали курса + кнопка покупки."""
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

    course = next((i for i in internships if i.get("code") == code), None)
    if not course:
        # Попробуем найти в общем каталоге
        all_courses = await aisystant.get_available_courses()
        course = next((c for c in all_courses if c.get("code") == code), None)

    if not course:
        await callback.message.answer(t('schedule.catalog_empty', lang))
        return

    name = course.get("courseName", course.get("name", code))
    amount = course.get("amount") or course.get("price", 0)

    if amount and amount > 0:
        text = t('schedule.payment_confirm', lang, course=name, amount=int(amount))
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('schedule.btn_pay', lang, amount=int(amount)),
                callback_data=f"schedule_pay:{code}:{int(amount)}",
            )],
            [InlineKeyboardButton(
                text=t('schedule.btn_cancel', lang),
                callback_data="schedule_courses",
            )],
        ])
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await callback.message.answer(f"*{name}*\n\nБесплатный курс.", parse_mode="Markdown")


@schedule_router.callback_query(F.data.startswith("schedule_pay:"))
async def callback_pay(callback: CallbackQuery):
    """Создать платёж за курс."""
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


def _build_catalog_text(courses: list[dict], lang: str) -> str:
    """Build formatted catalog text grouped by program."""
    sections: dict[str, list[dict]] = {}
    for course in courses:
        program = course.get("program", "personal")
        sections.setdefault(program, []).append(course)

    lines = [t('schedule.catalog_title', lang), ""]

    section_order = ['personal', 'professional', 'seminars', 'reviews']
    for section_key in section_order:
        section_courses = sections.get(section_key)
        if not section_courses:
            continue

        section_name = t(SECTION_NAMES.get(section_key, 'schedule.section_personal'), lang)
        lines.append(t('schedule.catalog_section', lang, section=section_name))

        for course in section_courses:
            name = course.get("courseName", course.get("code", "—"))
            start = _format_date(course.get("started", ""), lang)
            price = course.get("price")
            price_str = f"{int(price)} ₽" if price else "бесплатно"
            lines.append(t('schedule.course_item', lang, name=name, start=start, price=price_str))

        lines.append("")

    return "\n".join(lines)
