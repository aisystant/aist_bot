"""
Typing indicator — непрерывный «бот печатает...» на всё время долгой операции.

Использование:
    from helpers.typing_indicator import keep_typing

    await message.answer(t('loading_key', lang))
    async with keep_typing(message):
        data = await fetch_data()
        result = await claude.generate(...)
    # typing автоматически прекращается при выходе из блока

Как работает:
- При входе в `async with` сразу отправляет typing action
- Фоновая задача обновляет typing каждые 4 сек (TG typing длится ~5 сек)
- При выходе из блока (ответ готов или ошибка) задача отменяется → индикатор пропадает
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram.types import Message

logger = logging.getLogger(__name__)

TYPING_INTERVAL = 4  # секунд между обновлениями (typing длится ~5 сек)


@asynccontextmanager
async def keep_typing(message: Message):
    """Context manager: показывает typing indicator на всё время блока."""
    chat_id = message.chat.id
    bot = message.bot

    async def _loop():
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"typing indicator error: {e}")

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
