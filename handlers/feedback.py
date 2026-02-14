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
        await dispatcher.route_command('feedback', intern)
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))


@feedback_router.message(F.text.startswith("!"))
async def handle_quick_feedback(message: Message, state: FSMContext):
    """Quick feedback: !текст → сохранить как yellow bug report."""
    from handlers import get_dispatcher

    text = (message.text or "")[1:].strip()
    if not text:
        return  # Просто "!" без текста — игнорируем

    dispatcher = get_dispatcher()
    if not (dispatcher and dispatcher.is_sm_active):
        return

    intern = await get_intern(message.chat.id)
    if not intern:
        return

    await state.clear()
    await dispatcher.go_to(intern, "utility.feedback", context={
        'quick_message': text,
    })
