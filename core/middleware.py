"""
Middleware для aiogram.

LoggingMiddleware — логирование входящих сообщений.
TracingMiddleware — request-scoped трейсинг с записью в Neon.
"""

import asyncio
import logging

from aiogram import BaseMiddleware
from aiogram.enums import ChatAction
from aiogram.types import Message, CallbackQuery, TelegramObject

from core.tracing import start_trace, finish_trace

logger = logging.getLogger(__name__)


class MaintenanceMiddleware(BaseMiddleware):
    """Блокирует всех пользователей кроме ALLOWED_TESTERS.

    Включается переменной MAINTENANCE_MODE=true.
    Пользователям показывается сообщение с редиректом на основного бота.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict):
        from config.settings import MAINTENANCE_MODE, ALLOWED_TESTERS, MAINTENANCE_REDIRECT_BOT

        if not MAINTENANCE_MODE:
            return await handler(event, data)

        # Определяем chat_id
        chat_id = None
        if isinstance(event, Message) and event.from_user:
            chat_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            chat_id = event.from_user.id

        # Разрешённые тестировщики проходят
        if chat_id and chat_id in ALLOWED_TESTERS:
            return await handler(event, data)

        # Остальные получают сообщение
        if isinstance(event, Message) and chat_id:
            await event.answer(
                f"🔧 Этот бот используется для тестирования.\n\n"
                f"Пожалуйста, используйте основного бота: {MAINTENANCE_REDIRECT_BOT}"
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("🔧 Бот на тестировании", show_alert=True)

        return  # не пропускаем дальше


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

            # Typing indicator — мгновенная обратная связь пользователю
            try:
                await event.bot.send_chat_action(chat_id=event.chat.id, action=ChatAction.TYPING)
            except Exception:
                pass

            # Fire-and-forget: сохранить/обновить tg_username + снять bot_blocked
            if event.from_user:
                try:
                    from db.queries.users import clear_bot_blocked
                    asyncio.create_task(clear_bot_blocked(event.from_user.id))
                except Exception:
                    pass
                if event.from_user.username:
                    try:
                        from db.queries.users import update_tg_username
                        asyncio.create_task(update_tg_username(event.from_user.id, event.from_user.username))
                    except Exception:
                        pass

        elif isinstance(event, CallbackQuery) and event.message:
            # Typing для callbacks (кнопки «Подробнее», навигация)
            try:
                await event.bot.send_chat_action(
                    chat_id=event.message.chat.id, action=ChatAction.TYPING
                )
            except Exception:
                pass

        return await handler(event, data)


class TracingMiddleware(BaseMiddleware):
    """Middleware для трейсинга: замер полного времени обработки запроса.

    Создаёт Trace для каждого Message/CallbackQuery,
    записывает в Neon по завершении обработки.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict):
        # Определяем user_id и command
        user_id = 0
        command = "unknown"

        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else 0
            text = event.text or ""
            if text.startswith("/"):
                command = text.split()[0][:50]
            else:
                command = f"msg:{text[:30]}" if text else "msg:empty"
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else 0
            command = f"cb:{event.data[:40]}" if event.data else "cb:empty"
        else:
            # Для других типов событий — пропускаем трейсинг
            return await handler(event, data)

        # Определяем текущий SM state
        from aiogram.fsm.context import FSMContext
        state_ctx: FSMContext = data.get('state')
        sm_state = await state_ctx.get_state() if state_ctx else "unknown"

        # Создаём trace
        trace = start_trace(
            user_id=user_id,
            command=command,
            state=sm_state or "unknown",
        )

        try:
            result = await handler(event, data)
            return result
        finally:
            try:
                await finish_trace(trace)
            except Exception as e:
                logger.warning(f"[TracingMiddleware] Failed to finish trace: {e}")
            # Session tracking (fire-and-forget, не блокирует запрос)
            if user_id:
                try:
                    from db.queries.sessions import get_or_create_session
                    asyncio.create_task(get_or_create_session(user_id, command))
                except Exception:
                    pass
                # DAU: обновить last_active_date при любом взаимодействии
                try:
                    from db.queries.activity import touch_last_active_date
                    asyncio.create_task(touch_last_active_date(user_id))
                except Exception:
                    pass
