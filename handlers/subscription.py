"""
Подписка «Бесконечное развитие» через Aisystant (WP-79).

Команда: /subscription (кнопка «💳 Подписка» на T1 linked)
Показывает тарифы и создаёт платёж через Aisystant API.
"""

import logging

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command

from db.queries import get_intern
from db.queries.aisystant import get_aisystant_id
from clients.aisystant import aisystant
from i18n import t

logger = logging.getLogger(__name__)

subscription_router = Router(name="subscription")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@subscription_router.message(Command("subscription"))
async def cmd_subscription(message: Message):
    """Команда /subscription — оформление подписки БР."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await message.answer(t('aisystant_sub.no_account', lang))
        return

    # Проверяем, активна ли уже
    try:
        is_active = await aisystant.has_active_subscription(aisystant_id)
        if is_active:
            await message.answer(t('aisystant_sub.already_active', lang))
            return
    except Exception as e:
        logger.error(f"[Subscription] check error: {e}")

    # Получаем тарифы
    try:
        tariffs = await aisystant.get_subscription_tariffs(aisystant_id)
    except Exception as e:
        logger.error(f"[Subscription] get_tariffs error: {e}")
        await message.answer(t('aisystant_sub.error', lang))
        return

    if not tariffs:
        await message.answer(t('aisystant_sub.no_tariffs', lang))
        return

    lines = [
        t('aisystant_sub.title', lang),
        "",
        t('aisystant_sub.desc', lang),
        "",
    ]

    buttons = []
    for tariff in tariffs[:5]:
        code = tariff.get("code", "")
        name = tariff.get("name", code)
        amount = tariff.get("amount", 0)
        period = tariff.get("period", "месяц")

        lines.append(t('aisystant_sub.tariff_item', lang,
                        name=name, price=int(amount), period=period))

        if amount > 0:
            buttons.append([InlineKeyboardButton(
                text=f"💳 {name} — {int(amount)} ₽",
                callback_data=f"sub_pay:{code}:{int(amount)}",
            )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


@subscription_router.callback_query(F.data.startswith("sub_pay:"))
async def callback_sub_pay(callback: CallbackQuery):
    """Создать платёж за подписку БР."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    parts = callback.data.split(":")
    code = parts[1]
    amount = float(parts[2])

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('aisystant_sub.no_account', lang))
        return

    try:
        result = await aisystant.create_subscription_payment(aisystant_id, code, amount)
    except Exception as e:
        logger.error(f"[Subscription] create_payment error: {e}")
        await callback.message.answer(t('aisystant_sub.payment_error', lang))
        return

    if not result or not result.get("confirmationUrl"):
        await callback.message.answer(t('aisystant_sub.payment_error', lang))
        return

    url = result["confirmationUrl"]
    # Сбрасываем кэш подписки после инициации оплаты
    aisystant.invalidate_subscription_cache(aisystant_id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('aisystant_sub.btn_pay_link', lang), url=url)],
    ])
    await callback.message.answer(t('aisystant_sub.payment_success', lang), reply_markup=keyboard)


@subscription_router.callback_query(F.data == "aisystant_subscribe")
async def callback_aisystant_subscribe(callback: CallbackQuery):
    """Callback из paywall — перенаправляет на /subscription flow."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('aisystant_sub.no_account', lang))
        return

    # Показываем тарифы (и для новой подписки, и для продления)
    try:
        tariffs = await aisystant.get_subscription_tariffs(aisystant_id)
    except Exception as e:
        logger.error(f"[Subscription] get_tariffs error: {e}")
        await callback.message.answer(t('aisystant_sub.error', lang))
        return

    if not tariffs:
        await callback.message.answer(t('aisystant_sub.no_tariffs', lang))
        return

    lines = [t('aisystant_sub.title', lang), "", t('aisystant_sub.desc', lang), ""]
    buttons = []
    for tariff in tariffs[:5]:
        code = tariff.get("code", "")
        name = tariff.get("name", code)
        amount = tariff.get("amount", 0)
        period = tariff.get("period", "месяц")
        lines.append(t('aisystant_sub.tariff_item', lang, name=name, price=int(amount), period=period))
        if amount > 0:
            buttons.append([InlineKeyboardButton(
                text=f"💳 {name} — {int(amount)} ₽",
                callback_data=f"sub_pay:{code}:{int(amount)}",
            )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await callback.message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
