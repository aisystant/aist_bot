"""
Контактная информация (WP-79).

Команда: /contacts (кнопка «📞 Контакты» на T1 new)
"""

import logging

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

contacts_router = Router(name="contacts")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@contacts_router.message(Command("contacts"))
async def cmd_contacts(message: Message):
    """Команда /contacts — контактная информация."""
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    await message.answer(t('contacts.text', lang), parse_mode="Markdown")
