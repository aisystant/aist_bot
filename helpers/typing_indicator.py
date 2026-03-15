"""
Typing indicator helper — показывает «бот печатает...» перед долгими операциями.

Паттерн: после loading-сообщения → пауза TYPING_DELAY сек → typing action (5 сек).
Повторный вызов send_typing() перед Claude API / MCP / external API обновляет индикатор.

Использование:
    from helpers.typing_indicator import delayed_typing, send_typing

    await message.answer(t('loading_key', lang))
    await delayed_typing(message)           # sleep + typing
    ...подготовка данных...
    await send_typing(message)              # обновить перед тяжёлым вызовом
    result = await claude.generate(...)
"""

import asyncio

from aiogram.types import Message

TYPING_DELAY = 3  # секунд между loading-сообщением и typing indicator


async def send_typing(message: Message) -> None:
    """Отправляет chat action 'typing' (длится ~5 сек или до следующего сообщения)."""
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")


async def delayed_typing(message: Message, delay: float = TYPING_DELAY) -> None:
    """Пауза delay сек → typing action. Для вызова сразу после loading-сообщения."""
    await asyncio.sleep(delay)
    await send_typing(message)
