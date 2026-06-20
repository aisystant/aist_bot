"""
SafeBot — transport-layer Markdown → HTML intercept.

All parse_mode="Markdown" calls are automatically converted to HTML
via enforce_iwe_style() + md_to_html(). This eliminates
TelegramBadRequest: can't parse entities for the ENTIRE bot —
every send path goes through Bot methods.

Pipeline for parse_mode="Markdown":
  1. enforce_iwe_style() — em-dash IWE policy (replaces forbidden —)
  2. md_to_html()        — converts **bold**, #headers, `code` → safe HTML

Coverage:
- bot.send_message() → direct calls + message.answer() + message.reply()
- bot.edit_message_text() → direct calls + message.edit_text()

HTML parse_mode is deterministic: invalid markup shows as plain text,
never crashes.
"""

from aiogram import Bot
from aiogram.types import Message
from typing import Union

from helpers.markdown_to_html import md_to_html
from helpers.sanitize import enforce_iwe_style


class SafeBot(Bot):
    """Bot subclass that auto-converts Markdown → HTML at transport layer."""

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        **kwargs,
    ) -> Message:
        if kwargs.get('parse_mode') == 'Markdown':
            text = md_to_html(enforce_iwe_style(text))
            kwargs['parse_mode'] = 'HTML'
        return await super().send_message(chat_id, text, **kwargs)

    async def edit_message_text(
        self,
        text: str,
        **kwargs,
    ) -> Union[Message, bool]:
        if kwargs.get('parse_mode') == 'Markdown':
            text = md_to_html(enforce_iwe_style(text))
            kwargs['parse_mode'] = 'HTML'
        return await super().edit_message_text(text, **kwargs)
