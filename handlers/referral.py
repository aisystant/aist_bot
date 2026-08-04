"""Commands /invite and /invites — persistent referral flow, WP-266."""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import BOT_USERNAME
from db.queries import get_intern
from db.queries.invites import (
    InviteError,
    activate_invite,
    get_invite_stats,
    get_or_create_invite,
)

logger = logging.getLogger(__name__)
referral_router = Router(name="referral")

_ERROR_TEXT = {
    "invite_requires_active_subscription": (
        "Персональная ссылка доступна участникам с подпиской «Инженерия интеллекта»."
    ),
    "invite_not_found": "Эта ссылка приглашения не найдена.",
    "invite_self_referral": "Нельзя зарегистрироваться по собственной ссылке.",
    "invite_already_attributed": "Для твоего аккаунта приглашение уже было учтено.",
}


def _invite_url(code: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=invite_{code}"


async def activate_invite_for_user(message: Message, account_id: str, code: str) -> bool:
    try:
        attribution = await activate_invite(code, account_id)
    except InviteError as exc:
        await message.answer(_ERROR_TEXT.get(exc.code, "Не удалось учесть приглашение."))
        return False
    except Exception:
        logger.error("[invite] activate_invite failed code=%s account_id=%s", code, account_id, exc_info=True)
        await message.answer("Не удалось учесть приглашение — уже разбираемся.")
        return False

    from db.queries.onboarding_journey import write_referral_source

    try:
        await write_referral_source(account_id, str(attribution.granter_account_id))
    except Exception as exc:
        logger.warning(
            "[invite] referral attribution deferred id=%s: %s",
            attribution.attribution_id,
            exc,
        )

    await message.answer(
        "✅ <b>Приглашение учтено</b>\n\n"
        "Тексты руководств и марафон останутся доступны бесплатно и навсегда. "
        "Платные функции с ИИ-генерацией подключаются отдельно.\n\n"
        "Начать работу: /start",
        parse_mode="HTML",
    )
    return True


@referral_router.message(Command("invite"))
async def cmd_invite(message: Message):
    intern = await get_intern(message.chat.id)
    account_id = intern.get("dt_user_id") if intern else None
    if not account_id:
        await message.answer("Сначала подключи аккаунт Aisystant через /ory_register.")
        return

    try:
        link = await get_or_create_invite(account_id)
    except InviteError as exc:
        await message.answer(_ERROR_TEXT.get(exc.code, "Не удалось получить ссылку."))
        return
    except Exception:
        logger.error("[invite] get_or_create_invite failed account_id=%s", account_id, exc_info=True)
        await message.answer("Не удалось получить ссылку — уже разбираемся. Попробуй чуть позже.")
        return

    await message.answer(
        "🔗 <b>Твоя постоянная ссылка</b>\n\n"
        f"<code>{_invite_url(link.code)}</code>\n\n"
        "Отправляй её тем, кому могут быть полезны руководства и бот. "
        "Тексты руководств и марафон останутся доступны бесплатно и навсегда.\n\n"
        "Если приглашённый впервые оплатит подписку «Инженерия интеллекта», "
        "семинар или Марафон с наставником — тебе начислят <b>3000 бонусов</b>.\n\n"
        "Статистика приглашений: /invites",
        parse_mode="HTML",
    )


@referral_router.message(Command("invites"))
async def cmd_invites(message: Message):
    intern = await get_intern(message.chat.id)
    account_id = intern.get("dt_user_id") if intern else None
    if not account_id:
        await message.answer("Сначала подключи аккаунт Aisystant через /ory_register.")
        return

    try:
        stats = await get_invite_stats(account_id)
    except Exception:
        logger.error("[invite] get_invite_stats failed account_id=%s", account_id, exc_info=True)
        await message.answer("Не удалось получить статистику — уже разбираемся. Попробуй чуть позже.")
        return

    await message.answer(
        "📊 <b>Мои приглашения</b>\n\n"
        f"Приглашение учтено: <b>{stats['invited']}</b>\n"
        f"Совершили первую оплату: <b>{stats['paid']}</b>\n"
        f"Начислений по 3000: <b>{stats['rewards_count']}</b>\n"
        f"Всего начислено: <b>{stats['bonuses']:,.0f}</b> бонусов\n\n"
        "Получить свою ссылку: /invite",
        parse_mode="HTML",
    )
