"""
Обработка платежей Telegram Stars.

Подписка: createInvoiceLink → pre_checkout → successful_payment.
Grandfathering: Telegram auto-renews по исходной цене.
"""

import logging
from datetime import datetime, timezone

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


# === Subscribe callback (кнопка "Подписаться") ===

@payments_router.callback_query(F.data == "subscribe")
async def cb_subscribe(callback: CallbackQuery):
    """Создать invoice link и отправить пользователю."""
    await callback.answer()

    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    price = get_current_price()

    try:
        link = await callback.bot.create_invoice_link(
            title=t('subscription.invoice_title', lang),
            description=t('subscription.invoice_description', lang),
            payload=f"sub_{chat_id}_{price}",
            currency="XTR",
            prices=[LabeledPrice(label="Subscription", amount=price)],
            subscription_period=2592000,  # 30 дней
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('subscription.pay_button', lang, price=price),
                url=link,
            )]
        ])

        await callback.message.answer(
            t('subscription.invoice_text', lang, price=price),
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"[Payments] Error creating invoice: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.message.answer(t('errors.try_again', lang))


# === Pre-checkout: подтверждение платежа ===

@payments_router.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    """Должен ответить в течение 10 секунд. Всегда подтверждаем."""
    await pre_checkout_query.answer(ok=True)


# === Successful payment: запись подписки ===

@payments_router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    """Обработка успешного платежа — сохраняем подписку."""
    payment = message.successful_payment
    chat_id = message.chat.id

    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    try:
        # Извлекаем данные подписки
        charge_id = payment.telegram_payment_charge_id
        amount = payment.total_amount
        is_first = getattr(payment, 'is_first_recurring', False)

        # Дата окончания подписки
        expiration_ts = getattr(payment, 'subscription_expiration_date', None)
        if expiration_ts:
            expires_at = datetime.fromtimestamp(expiration_ts, tz=timezone.utc)
        else:
            # Fallback: 30 дней от сейчас
            from datetime import timedelta
            expires_at = datetime.now(timezone.utc) + timedelta(days=30)

        await save_subscription(
            chat_id=chat_id,
            charge_id=charge_id,
            stars_amount=amount,
            expires_at=expires_at,
            is_first=is_first,
        )

        # Определяем сообщение
        is_recurring = getattr(payment, 'is_recurring', False)
        if is_recurring and not is_first:
            msg_key = 'subscription.renewal_success'
        else:
            msg_key = 'subscription.success'

        await message.answer(t(msg_key, lang))
        logger.info(
            f"[Payments] Subscription saved: chat_id={chat_id}, "
            f"amount={amount} Stars, expires={expires_at}, "
            f"recurring={is_recurring}, first={is_first}"
        )

    except Exception as e:
        logger.error(f"[Payments] Error saving subscription: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await message.answer(t('subscription.success', lang))  # Всё равно сообщаем успех
