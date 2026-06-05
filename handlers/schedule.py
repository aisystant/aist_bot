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
- schedule_detail:{code}       — детали программы (legacy, backward compat)
- sched_pay_choice:{code}:{amount} — выбор: полная оплата / рассрочка (>35K)
- schedule_pay:{code}:{amount} — создание платежа (полная оплата)
- sched_pay_inst:{code}:{amount} — создание платежа в рассрочку (35%, paymentIndex=0)
- sub_pay_ws:{code}:{amount}   — платёж за мастерскую
"""

import asyncio
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

# Порог для показа выбора "полная оплата / рассрочка" (WP-5)
INSTALLMENT_THRESHOLD = 35_000

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
            return dt.strftime("%b %d, %Y %H:%M")
        return dt.strftime("%d.%m.%Y %H:%M")
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


async def _create_course_buttons(
    aisystant_id: str,
    paid_courses: list[tuple[str, str, int]],
    lang: str,
    emoji: str = "💳",
) -> list[list[InlineKeyboardButton]]:
    """Pre-create internship payments → URL buttons (no extra click).

    paid_courses: list of (code, short_name, amount).
    Для курсов >= INSTALLMENT_THRESHOLD — callback-кнопка с выбором способа оплаты.
    Falls back to callback buttons if payment creation fails.
    """
    async def _one(code: str, short_name: str, amount: int):
        # Дорогие курсы → промежуточный выбор (полная / рассрочка)
        if amount >= INSTALLMENT_THRESHOLD:
            return [InlineKeyboardButton(
                text=f"{emoji} {short_name} — {amount} ₽",
                callback_data=f"sched_pay_choice:{code}:{amount}",
            )]
        try:
            result = await aisystant.create_internship_payment(
                aisystant_id, code, amount,
            )
            if result and result.get("confirmationUrl"):
                return [InlineKeyboardButton(
                    text=f"{emoji} {short_name} — {amount} ₽",
                    url=result["confirmationUrl"],
                )]
        except Exception as e:
            logger.error(f"[Schedule] pre-create payment error for {code} amount={amount}: {e}")
        logger.warning(f"[Schedule] payment pre-create failed for {code} amount={amount}, hiding button")
        return []

    rows = await asyncio.gather(*[_one(c, n, a) for c, n, a in paid_courses])
    return [row for row in rows if row]


# ── Hub ─────────────────────────────────────────────────

async def _show_hub(message: Message, chat_id: int):
    """Показать навигационный хаб расписания."""
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    lines = []

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

    paid_courses = []
    for course in filtered:
        name = course.get("courseName", course.get("code", "—"))
        start = _format_date(course.get("started", ""), lang)
        price = course.get("price")
        price_str = f"{int(price)} ₽" if price else "бесплатно"
        lines.append(t('schedule.course_item', lang, name=name, start=start, price=price_str))

        if aisystant_id and price:
            code = course.get("code", "")
            btn_name = name.strip()
            if len(btn_name) > 30:
                btn_name = btn_name[:27] + "..."
            paid_courses.append((code, btn_name, int(price)))

    if paid_courses:
        lines.append(t('buy.payment_note', lang))

    # Сразу создаём платежи → URL-кнопки без лишнего шага
    if paid_courses:
        buttons.extend(await _create_course_buttons(aisystant_id, paid_courses, lang))

    # Кнопка «Витрина семинаров» — для категории seminars
    if category == 'seminars':
        buttons.append([InlineKeyboardButton(
            text="🎬 " + t('schedule.menu_showcase', lang),
            callback_data="showcase_main",
        )])

    # Кнопка «Назад»
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


@schedule_router.callback_query(F.data.startswith("sched_pay_choice:"))
async def callback_pay_choice(callback: CallbackQuery):
    """Выбор способа оплаты: полная / рассрочка (для курсов > INSTALLMENT_THRESHOLD)."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    parts = callback.data.split(":")
    code = parts[1]
    amount = int(parts[2])

    await callback.answer()

    # Получаем данные курса для отображения
    course_name = code
    course_data = None
    try:
        courses = await aisystant.get_available_courses()
        for c in courses:
            if c.get("code") == code:
                course_data = c
                course_name = c.get("courseName", c.get("name", code))
                break
    except Exception:
        pass

    # Собираем детальное сообщение (как на старом боте)
    text_parts = [t('schedule.pay_choice_header', lang, course=course_name)]

    if course_data:
        started = course_data.get("started", "")
        finished = course_data.get("finished", "")
        if started and finished:
            text_parts.append(t('schedule.pay_choice_dates', lang,
                                start=_format_date(started, lang),
                                end=_format_date(finished, lang)))

        chat_link = course_data.get("chatLink", "")
        if chat_link:
            text_parts.append(t('schedule.pay_choice_chat', lang, link=chat_link))

    aisystant_id = await get_aisystant_id(chat_id)
    installment_per = int(round(amount * 0.35))
    text_parts.append(t('schedule.pay_choice_price', lang, amount=amount))
    text = "\n\n".join(text_parts)

    # Pre-create платежи → URL-кнопки (полная + рассрочка)
    buttons = []

    # 1. Полная оплата — URL-кнопка (pre-create)
    if aisystant_id:
        try:
            full_result = await aisystant.create_internship_payment(
                aisystant_id, code, amount,
            )
            if full_result and full_result.get("confirmationUrl"):
                buttons.append([InlineKeyboardButton(
                    text=t('schedule.btn_pay_full', lang, amount=amount),
                    url=full_result["confirmationUrl"],
                )])
        except Exception as e:
            logger.error(f"[Schedule] pre-create full payment error for {code}: {e}")

    # Если pre-create не удался — кнопка полной оплаты недоступна, только рассрочка
    if not buttons:
        logger.warning(f"[Schedule] pay_choice: full payment pre-create failed for {code}, showing installment only")

    # 2. Рассрочка — callback (создаётся при нажатии, т.к. другая сумма)
    buttons.append([InlineKeyboardButton(
        text=t('schedule.btn_pay_installment', lang, amount=installment_per),
        callback_data=f"sched_pay_inst:{code}:{amount}",
    )])

    # 3. Назад
    buttons.append([InlineKeyboardButton(
        text=t('schedule.btn_back', lang),
        callback_data="sched_back",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as _edit_err:
        logger.warning(f"[Schedule] pay_choice edit_text failed for {code}: {_edit_err}")
        try:
            await callback.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as _ans_err:
            logger.error(f"[Schedule] pay_choice answer also failed for {code}: {_ans_err}")


@schedule_router.callback_query(F.data.startswith("sched_pay_inst:"))
async def callback_pay_installment(callback: CallbackQuery):
    """Создать платёж за программу в рассрочку (paymentIndex=0, 35% от полной стоимости)."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    parts = callback.data.split(":")
    code = parts[1]
    full_amount = float(parts[2])
    installment_amount = round(full_amount * 0.35)

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('schedule.no_account', lang))
        return

    try:
        result = await aisystant.create_internship_payment(
            aisystant_id, code, installment_amount, payment_index=0,
        )
    except Exception as e:
        logger.error(f"[Schedule] create_internship_payment (installment) error for {code}: {e}")
        await callback.message.answer(t('schedule.payment_error', lang))
        return

    if not result or not result.get("confirmationUrl"):
        logger.error(f"[Schedule] installment payment no confirmationUrl for {code}, result={result}")
        await callback.message.answer(t('schedule.payment_error', lang))
        return

    url = result["confirmationUrl"]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('schedule.btn_pay_link', lang), url=url)],
    ])
    msg = t('schedule.installment_success', lang, url=url)
    try:
        await callback.message.edit_text(msg, parse_mode="Markdown",
                                          reply_markup=keyboard,
                                          disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(msg, parse_mode="Markdown",
                                       reply_markup=keyboard,
                                       disable_web_page_preview=True)


@schedule_router.callback_query(F.data.startswith("schedule_pay:"))
async def callback_pay(callback: CallbackQuery):
    """Создать платёж за программу (полная оплата)."""
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
        logger.error(f"[Schedule] create_internship_payment error for {code}: {e}")
        await callback.message.answer(t('schedule.payment_error', lang))
        return

    if not result or not result.get("confirmationUrl"):
        logger.error(f"[Schedule] payment no confirmationUrl for {code}, amount={amount}, result={result}")
        await callback.message.answer(t('schedule.payment_error', lang))
        return

    url = result["confirmationUrl"]
    msg = t('schedule.payment_direct', lang, url=url)
    try:
        await callback.message.edit_text(msg, parse_mode="Markdown",
                                          reply_markup=None,
                                          disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(msg, parse_mode="Markdown",
                                       disable_web_page_preview=True)
