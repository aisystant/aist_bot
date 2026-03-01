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


PERIOD_LABELS = {
    'm': {'ru': '1 месяц', 'en': '1 month'},
    '3m': {'ru': '3 месяца', 'en': '3 months'},
    '6m': {'ru': '6 месяцев', 'en': '6 months'},
    'y': {'ru': '1 год', 'en': '1 year'},
    '2y': {'ru': '2 года', 'en': '2 years'},
}


def _parse_tariff(tariff: dict) -> tuple[str, str, int, str]:
    """Извлечь code, name, amount, periodicity из тарифа API.

    API формат: {"code": "...", "details": {"amount": 300, "periodicity": "m", "name": "..."}}
    """
    code = tariff.get("code", "")
    details = tariff.get("details", {})
    name = details.get("name", tariff.get("name", code))
    amount = details.get("amount", tariff.get("amount", 0))
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 0
    periodicity = details.get("periodicity", "m")
    return code, name, amount, periodicity


def _period_label(periodicity: str, lang: str) -> str:
    labels = PERIOD_LABELS.get(periodicity, {})
    return labels.get(lang, labels.get('ru', periodicity))


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
    paid_tariffs = []
    for tariff in tariffs[:5]:
        code, name, amount, periodicity = _parse_tariff(tariff)
        period = _period_label(periodicity, lang)
        lines.append(f"  • {period} — {amount} ₽")
        if amount > 0:
            paid_tariffs.append((code, amount, period))

    # Один тариф → сразу создаём платёж
    if len(paid_tariffs) == 1:
        code, amount, period = paid_tariffs[0]
        try:
            result = await aisystant.create_subscription_payment(aisystant_id, code, amount)
            if result and result.get("confirmationUrl"):
                url = result["confirmationUrl"]
                aisystant.invalidate_subscription_cache(aisystant_id)
                buttons.append([InlineKeyboardButton(
                    text=t('aisystant_sub.btn_pay_link', lang), url=url,
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"💳 {period} — {amount} ₽",
                    callback_data=f"sub_pay:{code}:{amount}",
                )])
        except Exception as e:
            logger.error(f"[Subscription] auto-payment error: {e}")
            buttons.append([InlineKeyboardButton(
                text=f"💳 {period} — {amount} ₽",
                callback_data=f"sub_pay:{code}:{amount}",
            )])
    else:
        for code, amount, period in paid_tariffs:
            buttons.append([InlineKeyboardButton(
                text=f"💳 {period} — {amount} ₽",
                callback_data=f"sub_pay:{code}:{amount}",
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
    paid_tariffs = []
    for tariff in tariffs[:5]:
        code, name, amount, periodicity = _parse_tariff(tariff)
        period = _period_label(periodicity, lang)
        lines.append(f"  • {period} — {amount} ₽")
        if amount > 0:
            paid_tariffs.append((code, amount, period))

    # Один тариф → сразу создаём платёж
    if len(paid_tariffs) == 1:
        code, amount, period = paid_tariffs[0]
        try:
            result = await aisystant.create_subscription_payment(aisystant_id, code, amount)
            if result and result.get("confirmationUrl"):
                url = result["confirmationUrl"]
                aisystant.invalidate_subscription_cache(aisystant_id)
                buttons.append([InlineKeyboardButton(
                    text=t('aisystant_sub.btn_pay_link', lang), url=url,
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"💳 {period} — {amount} ₽",
                    callback_data=f"sub_pay:{code}:{amount}",
                )])
        except Exception as e:
            logger.error(f"[Subscription] paywall auto-payment error: {e}")
            buttons.append([InlineKeyboardButton(
                text=f"💳 {period} — {amount} ₽",
                callback_data=f"sub_pay:{code}:{amount}",
            )])
    else:
        for code, amount, period in paid_tariffs:
            buttons.append([InlineKeyboardButton(
                text=f"💳 {period} — {amount} ₽",
                callback_data=f"sub_pay:{code}:{amount}",
            )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await callback.message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


@subscription_router.callback_query(F.data.startswith("sub_pay_ws:"))
async def callback_sub_pay_workshop(callback: CallbackQuery):
    """Создать платёж за подписку Мастерской (WORKSHOP)."""
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
        result = await aisystant.create_subscription_payment(
            aisystant_id, code, amount, purpose="WORKSHOP",
        )
    except Exception as e:
        logger.error(f"[Subscription] workshop payment error: {e}")
        await callback.message.answer(t('aisystant_sub.payment_error', lang))
        return

    if not result or not result.get("confirmationUrl"):
        await callback.message.answer(t('aisystant_sub.payment_error', lang))
        return

    url = result["confirmationUrl"]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('aisystant_sub.btn_pay_link', lang), url=url)],
    ])
    await callback.message.answer(t('aisystant_sub.payment_success', lang), reply_markup=keyboard)
