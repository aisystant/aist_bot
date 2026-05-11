"""
Юридические документы: /tos, /privacy.

/tos    — Условия использования IWE (B8.0)
/privacy — Политика конфиденциальности IWE (B8.0)

Тип C (микро): одна команда — одно сообщение со ссылкой + краткое резюме.
Связано: DS-ecosystem-development/docs/legal/, WP-212 B8.0
"""

import logging

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

legal_router = Router(name="legal")

TOS_URL = "https://system-school.ru/iwe/tos"
PRIVACY_URL = "https://system-school.ru/iwe/privacy"


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@legal_router.message(Command("tos"))
async def cmd_tos(message: Message):
    """Условия использования IWE v0.1."""
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    await message.answer(t('legal.tos_text', lang), parse_mode="Markdown")


@legal_router.message(Command("privacy"))
async def cmd_privacy(message: Message):
    """Политика конфиденциальности IWE v0.1."""
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    await message.answer(t('legal.privacy_text', lang), parse_mode="Markdown")
