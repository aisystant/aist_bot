"""
Хендлер обратной связи: /feedback + !текст (shortcut) + /reports (dev).

/feedback — полный flow через FeedbackState (SM)
!текст — быстрый баг-репорт (yellow, scenario=other)
/reports — список отчётов (только для DEVELOPER_CHAT_ID)
"""

import logging
import os

from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

feedback_router = Router(name="feedback")

_SEVERITY_ICON = {'red': '\U0001f534', 'yellow': '\U0001f7e1', 'green': '\U0001f7e2'}
_STATUS_ICON = {'new': '\U0001f195', 'notified': '\U0001f4e8', 'resolved': '\u2705'}


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
    """/reports — список отчётов (только для DEVELOPER_CHAT_ID)."""
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    if not dev_chat_id or str(message.chat.id) != dev_chat_id:
        return  # Молча игнорируем для не-разработчиков

    from db.queries.feedback import get_all_reports, get_report_stats

    try:
        stats = await get_report_stats()
        reports = await get_all_reports(limit=15)
    except Exception as e:
        logger.error(f"[Feedback] Error fetching reports: {e}")
        await message.answer("Error fetching reports.")
        return

    # Заголовок со статистикой
    text = (
        f"<b>Feedback Reports</b>\n"
        f"Total: {stats.get('total', 0)} | "
        f"\U0001f195 {stats.get('new_count', 0)} | "
        f"\U0001f4e8 {stats.get('notified_count', 0)} | "
        f"\u2705 {stats.get('resolved_count', 0)}\n"
        f"\U0001f534 {stats.get('red_count', 0)} | "
        f"\U0001f7e1 {stats.get('yellow_count', 0)} | "
        f"\U0001f7e2 {stats.get('green_count', 0)}\n"
        f"\u2500" * 20 + "\n"
    )

    if not reports:
        text += "\nNo reports yet."
    else:
        for r in reports:
            sev = _SEVERITY_ICON.get(r['severity'], '\u2753')
            st = _STATUS_ICON.get(r['status'], '\u2753')
            dt = r['created_at'].strftime('%d.%m %H:%M') if r.get('created_at') else '—'
            msg = (r.get('message') or '')[:80]
            scenario = r.get('scenario', 'other')
            text += f"\n{sev}{st} <b>#{r['id']}</b> | {scenario} | {dt}\n{msg}\n"

    # Telegram limit 4096
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"

    await message.answer(text, parse_mode="HTML")


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
