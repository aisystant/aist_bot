"""
Хендлер обратной связи: /feedback + !текст (shortcut).

/feedback — полный flow через FeedbackState (SM)
!текст — быстрый баг-репорт (yellow, scenario=other)
"""

import logging

from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

feedback_router = Router(name="feedback")


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
