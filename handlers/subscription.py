from __future__ import annotations

"""
Подписка «Инженерия интеллекта» через Aisystant (WP-79).

Команда: /subscription (кнопка «💳 Подписка» на T1 linked)
Показывает тарифы и создаёт платёж через Aisystant API.
"""

import asyncio
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
from db.queries.redeem import cancel_pending_reserve, confirm_subscription_reserves, update_reserve_payment_id
from clients.aisystant import aisystant
from helpers.dual_write import resolve_ory_id_from_chat
from helpers.redeem_helpers import (
    build_burn_offer_keyboard,
    prepare_burn_offer,
    reserve_for_yookassa_provisional,
)
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


async def _create_tariff_buttons(
    aisystant_id: str,
    paid_tariffs: list[tuple[str, int, str]],
    lang: str,
    purpose: str | None = None,
) -> list[list[InlineKeyboardButton]]:
    """Pre-create payments for all tariffs → URL buttons (no extra click).

    Falls back to callback buttons if payment creation fails.
    """
    async def _one(code: str, amount: int, period: str):
        try:
            result = await aisystant.create_subscription_payment(
                aisystant_id, code, amount,
                **({"purpose": purpose} if purpose else {}),
            )
            if result and result.get("confirmationUrl"):
                return [InlineKeyboardButton(
                    text=f"💳 {period} — {amount} ₽",
                    url=result["confirmationUrl"],
                )]
        except Exception as e:
            logger.error(f"[Subscription] pre-create payment error for {code}: {e}")
        # Fallback: callback button
        prefix = "sub_pay_ws" if purpose == "WORKSHOP" else "sub_pay"
        return [InlineKeyboardButton(
            text=f"💳 {period} — {amount} ₽",
            callback_data=f"{prefix}:{code}:{amount}",
        )]

    rows = await asyncio.gather(*[_one(c, a, p) for c, a, p in paid_tariffs])
    aisystant.invalidate_subscription_cache(aisystant_id)
    return list(rows)


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
            # Lazy-confirm: subscription webhook never reaches the bot (payment goes via
            # Aisystant), so pending SUBSCRIPTION reserves must be confirmed here.
            try:
                account_id = await resolve_ory_id_from_chat(chat_id)
                if account_id:
                    confirmed = await confirm_subscription_reserves(account_id)
                    if confirmed:
                        logger.info(f"[Subscription] lazy-confirmed {confirmed} reserve(s) for chat={chat_id}")
            except Exception as confirm_err:
                logger.warning(f"[Subscription] lazy-confirm failed, ignoring: {confirm_err}")
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

    lines.append(t('buy.payment_note', lang))

    # Сразу создаём платежи для всех тарифов → URL-кнопки без лишнего шага
    if paid_tariffs:
        buttons.extend(await _create_tariff_buttons(aisystant_id, paid_tariffs, lang))

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
    try:
        await callback.message.edit_text(
            t('aisystant_sub.payment_success', lang), reply_markup=keyboard,
        )
    except Exception:
        await callback.message.answer(
            t('aisystant_sub.payment_success', lang), reply_markup=keyboard,
        )


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

    lines.append(t('buy.payment_note', lang))

    # Сразу создаём платежи для всех тарифов → URL-кнопки без лишнего шага
    if paid_tariffs:
        buttons.extend(await _create_tariff_buttons(aisystant_id, paid_tariffs, lang))

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
    try:
        await callback.message.edit_text(
            t('aisystant_sub.payment_success', lang), reply_markup=keyboard,
        )
    except Exception:
        await callback.message.answer(
            t('aisystant_sub.payment_success', lang), reply_markup=keyboard,
        )


@subscription_router.callback_query(F.data == "subscribe_with_bonus")
async def callback_subscribe_with_bonus(callback: CallbackQuery):
    """Entry point from /points «💎 Подписка» — shows tariff list with per-tariff callbacks
    instead of pre-created payment URLs so the bonus flow can intercept each tariff click.
    """
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('aisystant_sub.no_account', lang))
        return

    try:
        tariffs = await aisystant.get_subscription_tariffs(aisystant_id)
    except Exception as e:
        logger.error(f"[Subscription] subscribe_with_bonus get_tariffs error: {e}")
        await callback.message.answer(t('aisystant_sub.error', lang))
        return

    if not tariffs:
        await callback.message.answer(t('aisystant_sub.no_tariffs', lang))
        return

    lines = [t('aisystant_sub.title', lang), "", t('aisystant_sub.desc', lang), ""]
    buttons = []
    for tariff in tariffs[:5]:
        code, name, amount, periodicity = _parse_tariff(tariff)
        period = _period_label(periodicity, lang)
        lines.append(f"  • {period} — {amount} ₽")
        if amount > 0:
            buttons.append([InlineKeyboardButton(
                text=f"💳 {period} — {amount} ₽",
                callback_data=f"sub_tariff:{code}:{amount}",
            )])

    lines.append(t('buy.payment_note', lang))
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await callback.message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


@subscription_router.callback_query(F.data.startswith("sub_tariff:"))
async def callback_sub_tariff(callback: CallbackQuery):
    """Tariff selected from subscribe_with_bonus — check bonus eligibility and branch."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    parts = callback.data.split(":")
    code = parts[1]
    amount = int(float(parts[2]))

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('aisystant_sub.no_account', lang))
        return

    burn_info = await prepare_burn_offer(chat_id, amount, skip_ceiling=True)

    if burn_info is None:
        # Not eligible — create payment directly (same as existing sub_pay flow)
        try:
            result = await aisystant.create_subscription_payment(aisystant_id, code, amount)
        except Exception as e:
            logger.error(f"[Subscription] sub_tariff direct payment error: code={code} {e}")
            await callback.message.answer(t('aisystant_sub.payment_error', lang))
            return

        if not result or not result.get("confirmationUrl"):
            await callback.message.answer(t('aisystant_sub.payment_error', lang))
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('aisystant_sub.btn_pay_link', lang), url=result["confirmationUrl"])],
        ])
        try:
            await callback.message.edit_text(t('aisystant_sub.payment_success', lang), reply_markup=keyboard)
        except Exception:
            await callback.message.answer(t('aisystant_sub.payment_success', lang), reply_markup=keyboard)
        return

    # Eligible for bonus — show burn offer.
    # Subscription payments go through Aisystant which validates amount == tariff price,
    # so we show the full card amount and frame bonus as a points deduction (cashback model).
    text = (
        f"💰 На копилке {int(burn_info['copilka_pts'])} бонусов.\n\n"
        f"Спишем <b>{int(burn_info['available_pts'])} бонусов</b> ({int(burn_info['discount_rub'])} ₽) "
        f"после подтверждения оплаты.\n"
        f"Степень: {burn_info['qualification']}\n"
        f"Оплата картой: <b>{amount} ₽</b>\n\n"
        f"Применить бонусы для подписки?"
    )
    keyboard = build_burn_offer_keyboard(
        apply_data=f"sub_burn_apply:{code}:{amount}",
        skip_data=f"sub_pay:{code}:{amount}",
        full_amount_rub=amount,
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@subscription_router.callback_query(F.data.startswith("sub_burn_apply:"))
async def callback_sub_burn_apply(callback: CallbackQuery):
    """User confirmed bonus redemption for subscription.

    Flow: provisional reserve → create Aisystant payment → promote reserve to real_id → show URL.
    On any error after reserve: cancel the provisional reserve before returning.
    """
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    parts = callback.data.split(":")
    code = parts[1]
    amount = int(float(parts[2]))

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)
    if not aisystant_id:
        await callback.message.answer(t('aisystant_sub.no_account', lang))
        return

    # Re-validate eligibility at execution time (amount may have changed between
    # sub_tariff and sub_burn_apply if a concurrent flow ran)
    burn_info = await prepare_burn_offer(chat_id, amount, skip_ceiling=True)
    if burn_info is None:
        logger.warning(f"[Subscription] sub_burn_apply: burn_info gone at apply time, chat={chat_id}")
        await callback.message.answer(t('aisystant_sub.payment_error', lang))
        return

    provisional_id, points_amount = await reserve_for_yookassa_provisional(burn_info, purpose="SUBSCRIPTION")
    if provisional_id is None:
        logger.warning(f"[Subscription] sub_burn_apply: reserve failed, chat={chat_id}")
        await callback.message.answer(t('aisystant_sub.payment_error', lang))
        return

    # Create real payment at full tariff price — Aisystant validates amount == tariff.price
    # and rejects discounted amounts with 403. Bonus deduction happens via lazy-confirm
    # in cmd_subscription when has_active_subscription() fires (cashback model).
    try:
        result = await aisystant.create_subscription_payment(
            aisystant_id, code, float(amount),
        )
    except Exception as e:
        logger.error(f"[Subscription] sub_burn_apply: create_payment failed, cancelling reserve: {e}")
        await cancel_pending_reserve(provisional_id)
        await callback.message.answer(t('aisystant_sub.payment_error', lang))
        return

    if not result or not result.get("confirmationUrl") or not result.get("id"):
        logger.error(f"[Subscription] sub_burn_apply: bad payment response, cancelling reserve: {result}")
        await cancel_pending_reserve(provisional_id)
        await callback.message.answer(t('aisystant_sub.payment_error', lang))
        return

    real_id = result["id"]
    promoted = await update_reserve_payment_id(provisional_id, real_id)
    if not promoted:
        logger.error(
            f"[Subscription] sub_burn_apply: update_reserve_payment_id failed "
            f"provisional={provisional_id} real={real_id} — cancelling reserve"
        )
        await cancel_pending_reserve(provisional_id)
        await callback.message.answer(t('aisystant_sub.payment_error', lang))
        return

    aisystant.invalidate_subscription_cache(aisystant_id)

    text = (
        f"✅ <b>{points_amount:.0f} бонусов</b> спишутся после подтверждения оплаты.\n\n"
        + t('aisystant_sub.payment_success', lang)
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('aisystant_sub.btn_pay_link', lang), url=result["confirmationUrl"])],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
