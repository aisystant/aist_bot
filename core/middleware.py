"""
Middleware для aiogram.
"""

import logging

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    """Middleware для логирования всех входящих сообщений"""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        from aiogram.fsm.context import FSMContext

        if isinstance(event, Message):
            state: FSMContext = data.get('state')
            current_state = await state.get_state() if state else None
            logger.info(f"[MIDDLEWARE] Получено сообщение: chat_id={event.chat.id}, "
                       f"user_id={event.from_user.id if event.from_user else None}, "
                       f"text={event.text[:50] if event.text else '[no text]'}, "
                       f"state={current_state}")

        return await handler(event, data)
