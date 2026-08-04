"""Команды /invite и /referral — гостевой пропуск WP-266."""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from db.queries import get_intern
from db.queries.guest_pass import GuestPassError, activate_guest_pass, create_guest_pass
from config import BOT_USERNAME

logger = logging.getLogger(__name__)

referral_router = Router(name="referral")


_ERROR_TEXT = {
    "guest_pass_requires_active_subscription": (
        "Гостевые пропуска доступны участникам с действующей подпиской."
    ),
    "guest_pass_open_quota_reached": (
        "У тебя уже есть 5 открытых пропусков. Дождись активации или истечения одного из них."
    ),
    "guest_pass_not_found": "Этот гостевой пропуск не найден.",
    "guest_pass_not_available": "Этот гостевой пропуск уже использован или отозван.",
    "guest_pass_expired": "Срок действия этой ссылки истёк.",
    "guest_pass_self_referral": "Нельзя активировать собственный гостевой пропуск.",
    "guest_pass_already_used": "Ты уже использовал гостевой пропуск ранее.",
    "guest_pass_recipient_has_access": "У тебя уже есть действующий полный доступ.",
}


async def activate_guest_pass_for_user(message: Message, account_id: str, token: str) -> bool:
    """Activate access and persist canonical referral attribution."""
    try:
        activation = await activate_guest_pass(token, account_id)
    except GuestPassError as exc:
        await message.answer(_ERROR_TEXT.get(exc.code, "Не удалось активировать пропуск."))
        return False

    from db.queries.onboarding_journey import write_referral_source

    try:
        await write_referral_source(account_id, str(activation.granter_account_id))
    except Exception as exc:
        # Доступ уже выдан атомарно в subscription DB; Guest Pass сохраняет
        # granter/recipient для восстановления атрибуции без потери активации.
        logger.warning(
            "[guest-pass] referral attribution deferred pass=%s: %s",
            activation.pass_id,
            exc,
        )
    await message.answer(
        "🎟 <b>Гостевой пропуск активирован</b>\n\n"
        f"Полный доступ открыт до <b>{activation.access_valid_to:%d.%m.%Y}</b>.\n"
        "Можно начинать: /start",
        parse_mode="HTML",
    )
    logger.info(
        "[guest-pass] activated pass=%s recipient=%s granter=%s",
        activation.pass_id,
        account_id[:8],
        str(activation.granter_account_id)[:8],
    )
    return True


@referral_router.message(Command("invite"))
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

    try:
        guest_pass = await create_guest_pass(ory_uuid)
    except GuestPassError as exc:
        await message.answer(_ERROR_TEXT.get(exc.code, "Не удалось создать гостевой пропуск."))
        return

    link = f"https://t.me/{BOT_USERNAME}?start=guest_{guest_pass.token}"
    logger.info("[guest-pass] issued chat_id=%s pass=%s", chat_id, guest_pass.pass_id)

    await message.answer(
        f"🎟 <b>Гостевой пропуск на 14 дней</b>\n\n"
        f"<code>{link}</code>\n\n"
        "Отправь ссылку одному другу. Она одноразовая и действует 14 дней. "
        "Одновременно можно держать открытыми до пяти пропусков.",
        parse_mode="HTML",
    )
