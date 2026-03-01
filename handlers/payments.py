"""
Обработка донатов через Telegram Stars.

Два варианта:
- donate_once: разовый донат (без subscription_period)
- donate_recurring: ежемесячный донат (subscription_period=30 дней)

Подписка на Aisystant «Бесконечное развитие» → handlers/subscription.py (определяет тир T2).
"""

import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    PreCheckoutQuery,
    LabeledPrice,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from core.pricing import get_current_price
from db.queries import get_intern
from db.queries.subscription import save_subscription, get_active_subscription
from i18n import t

logger = logging.getLogger(__name__)

payments_router = Router(name="payments")


# === Разовый донат ===

@payments_router.callback_query(F.data == "donate_once")
async def cb_donate_once(callback: CallbackQuery):
    """Создать invoice для разового доната."""
    await callback.answer()

    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    price = get_current_price()

    try:
        link = await callback.bot.create_invoice_link(
            title=t('donation.once_invoice_title', lang),
            description=t('donation.once_invoice_description', lang),
            payload=f"donate_once_{chat_id}_{price}",
            currency="XTR",
            prices=[LabeledPrice(label="Donation", amount=price)],
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('donation.once_pay_button', lang, price=price),
                url=link,
            )]
        ])

        await callback.message.answer(
            t('donation.once_invoice_text', lang, price=price),
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"[Payments] Error creating one-time donation invoice: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.message.answer(t('errors.try_again', lang))


# === Постоянный (ежемесячный) донат ===

@payments_router.callback_query(F.data == "donate_recurring")
async def cb_donate_recurring(callback: CallbackQuery):
    """Создать invoice для ежемесячного доната."""
    await callback.answer()

    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    price = get_current_price()

    try:
        link = await callback.bot.create_invoice_link(
            title=t('donation.recurring_invoice_title', lang),
            description=t('donation.recurring_invoice_description', lang),
            payload=f"sub_{chat_id}_{price}",
            currency="XTR",
            prices=[LabeledPrice(label="Monthly donation", amount=price)],
            subscription_period=2592000,  # 30 дней
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('donation.recurring_pay_button', lang, price=price),
                url=link,
            )]
        ])

        await callback.message.answer(
            t('donation.recurring_invoice_text', lang, price=price),
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"[Payments] Error creating recurring donation invoice: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.message.answer(t('errors.try_again', lang))


# === Backward compatibility: старые кнопки "Подписаться" из уведомлений ===

@payments_router.callback_query(F.data == "subscribe")
async def cb_subscribe_legacy(callback: CallbackQuery):
    """Legacy: старые кнопки подписки → перенаправляем на ежемесячный донат."""
    await cb_donate_recurring(callback)


# === Pre-checkout: подтверждение платежа ===

@payments_router.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    """Должен ответить в течение 10 секунд. Всегда подтверждаем."""
    await pre_checkout_query.answer(ok=True)


# === Successful payment ===

@payments_router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    """Обработка успешного платежа — разовый донат или ежемесячный."""
    payment = message.successful_payment
    chat_id = message.chat.id

    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    payload = getattr(payment, 'invoice_payload', '') or ''

    # Разовый донат — благодарим и не сохраняем подписку
    if payload.startswith("donate_once_"):
        amount = payment.total_amount
        await message.answer(t('donation.once_success', lang))
        logger.info(f"[Payments] One-time donation: chat_id={chat_id}, amount={amount} Stars")
        return

    # Ежемесячный донат — сохраняем как подписку (для отслеживания и отмены)
    try:
        charge_id = payment.telegram_payment_charge_id
        amount = payment.total_amount
        is_first = getattr(payment, 'is_first_recurring', False)

        expiration_ts = getattr(payment, 'subscription_expiration_date', None)
        if expiration_ts:
            expires_at = datetime.utcfromtimestamp(expiration_ts)
        else:
            from datetime import timedelta
            expires_at = datetime.utcnow() + timedelta(days=30)

        await save_subscription(
            chat_id=chat_id,
            charge_id=charge_id,
            stars_amount=amount,
            expires_at=expires_at,
            is_first=is_first,
        )

        is_recurring = getattr(payment, 'is_recurring', False)
        if is_recurring and not is_first:
            msg_key = 'donation.recurring_renewal'
        else:
            msg_key = 'donation.recurring_success'

        await message.answer(t(msg_key, lang))
        logger.info(
            f"[Payments] Recurring donation saved: chat_id={chat_id}, "
            f"amount={amount} Stars, expires={expires_at}, "
            f"recurring={is_recurring}, first={is_first}"
        )

    except Exception as e:
        logger.error(f"[Payments] Error saving recurring donation: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await message.answer(t('donation.recurring_success', lang))
