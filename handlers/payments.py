"""
Обработка донатов через Telegram Stars.

WP-85: Stars = донаты (благодарность), НЕ влияют на тир/доступ.
Подписка «Инженерия интеллекта» (определяет T2) → handlers/subscription.py.

Два варианта донатов:
- donate_once: разовый донат (без subscription_period)
- donate_recurring: ежемесячный донат (subscription_period=30 дней)

WP-231 Ф-H: при ежемесячном донате (sub_ payload) дополнительно пишем
в subscription_grants (platform БД) — право доступа к Gateway по telegram_id.
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

from core.operator_alerts import alert_recurring_donation_failure
from core.pricing import get_current_price
from db.queries import get_intern
from db.queries.subscription import save_subscription, get_active_subscription, upsert_subscription_grant
from i18n import t

logger = logging.getLogger(__name__)

payments_router = Router(name="payments")


# === Разовый донат (с выбором суммы) ===

@payments_router.callback_query(F.data.startswith("donate_pay:"))
async def cb_donate_pay(callback: CallbackQuery):
    """Создать invoice для разового доната на указанную сумму."""
    await callback.answer()

    chat_id = callback.message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    try:
        amount = int(callback.data.split(":")[1])
        if amount < 1 or amount > 10000:
            raise ValueError
    except (ValueError, IndexError):
        await callback.message.answer(t('errors.try_again', lang))
        return

    try:
        link = await callback.bot.create_invoice_link(
            title=t('donation.once_invoice_title', lang),
            description=t('donation.once_invoice_description', lang),
            payload=f"donate_once_{chat_id}_{amount}",
            currency="XTR",
            prices=[LabeledPrice(label="Donation", amount=amount)],
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('donation.once_pay_button', lang, price=amount),
                url=link,
            )]
        ])

        await callback.message.answer(
            t('donation.once_invoice_text', lang, price=amount),
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
    """Legacy: старые кнопки подписки → перенаправляем на подписку Aisystant.

    WP-85: Stars = донаты. Подписка = «Инженерия интеллекта» на Aisystant.
    """
    from handlers.subscription import callback_aisystant_subscribe
    await callback_aisystant_subscribe(callback)


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

    # A language lookup must not prevent recovery of an already charged payment.
    lang = "ru"
    try:
        intern = await get_intern(chat_id)
        lang = (intern or {}).get("language", "ru") or "ru"
    except Exception as exc:  # noqa: BLE001 - Storage recovery must still run.
        logger.warning("[Payments] Language lookup failed (%s)", type(exc).__name__)

    payload = getattr(payment, "invoice_payload", "") or ""

    # Разовый донат — благодарим и предлагаем ежемесячный
    if payload.startswith("donate_once_"):
        amount = payment.total_amount
        await message.answer(t("donation.once_success", lang))
        logger.info(
            f"[Payments] One-time donation: chat_id={chat_id}, amount={amount} Stars"
        )

        # Предложить сделать донат постоянным (всегда, даже при активной подписке)
        try:
            link = await message.bot.create_invoice_link(
                title=t('donation.recurring_invoice_title', lang),
                description=t('donation.recurring_invoice_description', lang),
                payload=f"sub_{chat_id}_{amount}",
                currency="XTR",
                prices=[LabeledPrice(label="Monthly donation", amount=amount)],
                subscription_period=2592000,
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('donation.suggest_recurring_button', lang),
                    url=link,
                )]
            ])
            await message.answer(
                t('donation.suggest_recurring', lang),
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"[Payments] Error suggesting recurring after one-time: {e}")

        return

    # Ежемесячный донат — сохраняем как подписку (для отслеживания и отмены)
    charge_id = payment.telegram_payment_charge_id
    amount = payment.total_amount
    is_first = getattr(payment, 'is_first_recurring', False)

    expiration_ts = getattr(payment, 'subscription_expiration_date', None)
    if expiration_ts:
        expires_at = datetime.utcfromtimestamp(expiration_ts)
    else:
        from datetime import timedelta
        expires_at = datetime.utcnow() + timedelta(days=30)

    # WP-567 Ф3в: только сама запись в БД триггерит "не сохранён" -- раньше
    # один try оборачивал и это, и отправку ответа пользователю, так что
    # упавший message.answer() ПОСЛЕ успешного save_subscription тоже уходил
    # в except и слал ложный алерт "не сохранён" (запись-то реально была).
    try:
        await save_subscription(
            chat_id=chat_id,
            charge_id=charge_id,
            stars_amount=amount,
            expires_at=expires_at,
            is_first=is_first,
        )
    except Exception as e:
        logger.error(
            "[Payments] Recurring donation was not saved (%s)", type(e).__name__
        )
        await alert_recurring_donation_failure(
            message.bot, chat_id=chat_id, charge_id=charge_id
        )
        # НЕ отвечаем "успешно" при реальном сбое сохранения -- раньше
        # `donation.recurring_success` отправлялся при ЛЮБОМ исключении,
        # включая упавший save_subscription (пользователь считал донат
        # сохранённым, хотя записи не было). Инлайн-текст, не через t(...) --
        # новый i18n-ключ пришлось бы добавлять во все локали ради редкого
        # аварийного пути.
        await message.answer(
            "⚠️ Донат получен, но обработка занимает больше времени, чем обычно. "
            "Если статус не обновится в течение нескольких минут — напишите в поддержку."
        )
        return

    # WP-231 Ф-H: фиксируем право доступа к Gateway по telegram_id
    # Fire-and-forget — ошибка не должна ломать основной flow
    try:
        await upsert_subscription_grant(
            telegram_id=chat_id,
            valid_until=expires_at,
            source='tg_stars',
        )
    except Exception as grant_err:
        logger.error(f"[Payments] Failed to upsert subscription_grant: {grant_err}")

    is_recurring = getattr(payment, 'is_recurring', False)
    if is_recurring and not is_first:
        msg_key = 'donation.recurring_renewal'
    else:
        msg_key = 'donation.recurring_success'

    try:
        await message.answer(t(msg_key, lang))
    except Exception as answer_err:
        # Подписка уже сохранена -- сбой здесь чисто UX (пользователь не
        # увидит подтверждение), не повод для "не сохранён"-алерта.
        logger.error(f"[Payments] Failed to send success message (subscription WAS saved): {answer_err}")
    logger.info(
        f"[Payments] Recurring donation saved: chat_id={chat_id}, "
        f"amount={amount} Stars, expires={expires_at}, "
        f"recurring={is_recurring}, first={is_first}"
    )
