"""
Хендлер обратной связи: /feedback + !текст (shortcut) + /reports (dev).

/feedback — полный flow через FeedbackState (SM)
!текст — быстрый баг-репорт (yellow, scenario=other)
/reports — список отчётов (только для DEVELOPER_CHAT_ID)
"""

import logging
import os

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

feedback_router = Router(name="feedback")

_SEVERITY_ICON = {'red': '\U0001f534', 'yellow': '\U0001f7e1', 'green': '\U0001f7e2'}
_STATUS_ICON = {'new': '\U0001f195', 'notified': '\U0001f4e8', 'resolved': '\u2705'}


def _reports_keyboard(active: str = "") -> InlineKeyboardMarkup:
    """Inline-кнопки для /reports."""
    def label(text, key):
        return f"\u25cf {text}" if active == key else text

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label("Day", "day"), callback_data="reports:day"),
        InlineKeyboardButton(text=label("Week", "week"), callback_data="reports:week"),
        InlineKeyboardButton(text=label("All", "all"), callback_data="reports:all"),
        InlineKeyboardButton(text="\U0001f5d1 Clear", callback_data="reports:clear"),
    ]])


async def _render_reports(since_hours: int = None, period_label: str = "All time") -> str:
    """Формирует HTML-текст отчётов."""
    from db.queries.feedback import get_all_reports, get_report_stats

    stats = await get_report_stats()
    reports = await get_all_reports(limit=20, since_hours=since_hours)

    separator = "\u2500" * 20
    text = (
        f"<b>Feedback Reports</b> | {period_label}\n"
        f"Total: {stats.get('total', 0)} | "
        f"\U0001f195 {stats.get('new_count', 0)} | "
        f"\U0001f4e8 {stats.get('notified_count', 0)} | "
        f"\u2705 {stats.get('resolved_count', 0)}\n"
        f"\U0001f534 {stats.get('red_count', 0)} | "
        f"\U0001f7e1 {stats.get('yellow_count', 0)} | "
        f"\U0001f7e2 {stats.get('green_count', 0)}\n"
        f"{separator}\n"
    )

    if not reports:
        text += "\nNo reports."
    else:
        for r in reports:
            sev = _SEVERITY_ICON.get(r['severity'], '\u2753')
            st = _STATUS_ICON.get(r['status'], '\u2753')
            dt = r['created_at'].strftime('%d.%m %H:%M') if r.get('created_at') else '\u2014'
            msg = (r.get('message') or '')[:80]
            scenario = r.get('scenario', 'other')
            user_name = r.get('user_name') or f"#{r['chat_id']}"
            text += f"\n{sev}{st} <b>#{r['id']}</b> | {user_name} | {scenario} | {dt}\n{msg}\n"

    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"

    return text


@feedback_router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    """/feedback — открыть форму обратной связи через SM."""
    from handlers import get_dispatcher

    dispatcher = get_dispatcher()
    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await state.clear()
        logger.info(f"[Feedback] /feedback from chat_id={message.chat.id}, current_state={intern.get('current_state')}")
        try:
            await dispatcher.route_command('feedback', intern)
        except Exception as e:
            logger.error(f"[Feedback] Error routing /feedback: {e}")
            import traceback
            logger.error(traceback.format_exc())
            lang = intern.get('language', 'ru') or 'ru'
            await message.answer(t('errors.processing_error', lang))
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))


@feedback_router.message(Command("reports"))
async def cmd_reports(message: Message):
    """/reports — отчёты с inline-кнопками (только DEVELOPER_CHAT_ID)."""
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    if not dev_chat_id or str(message.chat.id) != dev_chat_id:
        return

    try:
        text = await _render_reports(period_label="All time")
    except Exception as e:
        logger.error(f"[Feedback] Error fetching reports: {e}")
        await message.answer("Error fetching reports.")
        return

    await message.answer(text, parse_mode="HTML", reply_markup=_reports_keyboard(active="all"))


@feedback_router.callback_query(F.data.startswith("reports:"))
async def cb_reports(callback: CallbackQuery):
    """Inline-кнопки отчётов: day/week/all/clear."""
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    if not dev_chat_id or str(callback.from_user.id) != dev_chat_id:
        await callback.answer()
        return

    action = callback.data.split(":", 1)[1]
    await callback.answer()

    if action == "clear":
        from db.queries.feedback import clear_all_reports
        try:
            count = await clear_all_reports()
            text = await _render_reports(period_label="All time")
            text = f"\u2705 Deleted {count} reports.\n\n{text}"
        except Exception as e:
            logger.error(f"[Feedback] Error clearing reports: {e}")
            text = "Error clearing reports."
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_reports_keyboard(active="all"))
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=_reports_keyboard(active="all"))
        return

    params = {
        "day": (24, "Today (24h)"),
        "week": (168, "This week (7d)"),
        "all": (None, "All time"),
    }
    since_hours, period_label = params.get(action, (None, "All time"))

    try:
        text = await _render_reports(since_hours=since_hours, period_label=period_label)
    except Exception as e:
        logger.error(f"[Feedback] Error fetching reports: {e}")
        text = "Error fetching reports."

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_reports_keyboard(active=action))
    except Exception:
        pass


@feedback_router.message(F.text.startswith("!"))
async def handle_quick_feedback(message: Message, state: FSMContext):
    """Quick feedback: !текст → сохранить напрямую в БД (без SM-переходов)."""
    text = (message.text or "")[1:].strip()
    if not text:
        return  # Просто "!" без текста — игнорируем

    intern = await get_intern(message.chat.id)
    if not intern:
        return

    chat_id = message.chat.id
    lang = intern.get('language', 'ru') or 'ru'
    logger.info(f"[Feedback] !shortcut from chat_id={chat_id}, text={text[:50]}")

    try:
        from db.queries.feedback import save_feedback
        report_id = await save_feedback(
            chat_id=chat_id,
            category='bug',
            scenario='other',
            severity='yellow',
            message=text,
        )
        if report_id:
            await message.answer(t('feedback.quick_saved', lang, id=report_id))
        else:
            await message.answer(t('feedback.error', lang))
    except Exception as e:
        logger.error(f"[Feedback] Error saving !shortcut: {e}")
        await message.answer(t('feedback.error', lang))
