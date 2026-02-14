"""
Хендлер быстрой обратной связи (!текст).

Аналогичен handlers/github.py (.текст для заметок).
"""

import logging

from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from db.queries import get_intern

logger = logging.getLogger(__name__)

feedback_router = Router(name="feedback")


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
