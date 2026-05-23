"""
Команда /referral — персональная реферальная ссылка (WP-349 Ф20).

Генерирует deeplink на основе ory_uuid пользователя.
Когда получатель откроет ссылку, Ф20 зафиксирует referral_source в onboarding_state.
"""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from db.queries import get_intern
from config import BOT_USERNAME

logger = logging.getLogger(__name__)

referral_router = Router(name="referral")


@referral_router.message(Command("referral"))
async def cmd_referral(message: Message):
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    if not intern:
        return

    ory_uuid = intern.get('dt_user_id')

    if not ory_uuid:
        await message.answer(
            "Чтобы получить реферальную ссылку, сначала подключи аккаунт Aisystant — "
            "используй /ory_register.",
        )
        return

    link = f"https://t.me/{BOT_USERNAME}?start=ref_{ory_uuid}"
    logger.info("[referral] link generated chat_id=%s uuid_prefix=%s", chat_id, ory_uuid[:8])

    await message.answer(
        f"🔗 Твоя персональная ссылка:\n\n"
        f"<code>{link}</code>\n\n"
        f"Отправь другу — когда он зарегистрируется через неё, мы зафиксируем что он пришёл от тебя.",
        parse_mode="HTML",
    )
