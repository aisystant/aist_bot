"""
SafeBot — transport-layer Markdown → HTML intercept.

All parse_mode="Markdown" calls are automatically converted to HTML
via md_to_html(). This eliminates TelegramBadRequest: can't parse entities
for the ENTIRE bot — every send path goes through Bot methods.

Coverage:
- bot.send_message() → direct calls + message.answer() + message.reply()
- bot.edit_message_text() → direct calls + message.edit_text()

HTML parse_mode is deterministic: invalid markup shows as plain text,
never crashes.
"""

from aiogram import Bot
from aiogram.types import Message
from typing import Union


class SafeBot(Bot):
    """Bot subclass that auto-converts Markdown → HTML at transport layer."""

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        **kwargs,
    ) -> Message:
        if kwargs.get('parse_mode') == 'Markdown':
            from helpers.markdown_to_html import md_to_html
            text = md_to_html(text)
            kwargs['parse_mode'] = 'HTML'
        return await super().send_message(chat_id, text, **kwargs)

    async def edit_message_text(
        self,
        text: str,
        **kwargs,
    ) -> Union[Message, bool]:
        if kwargs.get('parse_mode') == 'Markdown':
            from helpers.markdown_to_html import md_to_html
            text = md_to_html(text)
            kwargs['parse_mode'] = 'HTML'
        return await super().edit_message_text(text, **kwargs)
