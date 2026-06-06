from __future__ import annotations

"""
Stars-подписки «Инженерия интеллекта» (WP-246 Ф1.1).

Поток:
  /subscribe_stars → выбор тарифа → invoice Stars →
  pre_checkout_query → successful_payment →
    emit payment_received (→ payment.payment_received via projection-worker) +
    emit subscription_granted (→ subscription.contract via projection-worker) +
    save_subscription (FSM-state, Railway Postgres, TTL≈24h)

Архитектурные решения (Q16, Q18 WP-246):
- Handler НЕ пишет permanent-данные в bot.subscriptions.
- Permanent-state = subscription.contract в Neon (через event-gateway + projection-worker).
- bot.subscriptions = FSM-only (idempotency marker, TTL≈24h).
- source в event-gateway = "aist-bot" (уже в ALLOWED_SOURCES wrangler.toml).

Payload-формат invoice: "stars_sub_{chat_id}_{tariff_key}"
  tariff_key: "1m" | "3m" | "6m" | "12m"

# see DP.SC.120 (Payment Receiver), WP-246 Ф1.1
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
)

from db.queries import get_intern
from db.queries.subscription import save_subscription
from helpers.dual_write import post_event, resolve_ory_id_from_chat
from i18n import t

logger = logging.getLogger(__name__)

subscription_stars_router = Router(name="subscription_stars")

# ── Тарифы Stars (XTR) ───────────────────────────────────────────────────────
# Цены по WP-246 Ф1.1 spec. Названия = имена в reference.tariffs (lookup ключ).
STARS_TARIFFS = {
    "1m":  {"stars": 500,  "months": 1,  "name": "ЛР 1 мес (Stars)",  "label": "1 месяц — 500 Stars"},
    "3m":  {"stars": 1350, "months": 3,  "name": "ЛР 3 мес (Stars)",  "label": "3 месяца — 1350 Stars"},
    "6m":  {"stars": 2400, "months": 6,  "name": "ЛР 6 мес (Stars)",  "label": "6 месяцев — 2400 Stars"},
    "12m": {"stars": 3600, "months": 12, "name": "ЛР 12 мес (Stars)", "label": "12 месяцев — 3600 Stars"},
}

EVENT_SOURCE = "aist-bot"  # уже в ALLOWED_SOURCES event-gateway


def _lang(intern) -> str:
    if not intern:
        return "ru"
    return intern.get("language", "ru") or "ru"


# ── /subscribe_stars ─────────────────────────────────────────────────────────

@subscription_stars_router.message(Command("subscribe_stars"))
async def cmd_subscribe_stars(message: Message):
    """Команда /subscribe_stars — меню выбора тарифа (Stars)."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    text = (
        "⭐ *Подписка «Инженерия интеллекта» через Telegram Stars*\n\n"
        "Stars — встроенная валюта Telegram. Оплата без карты, мгновенно.\n\n"
        "Выберите период:"
    )

    buttons = [
        [InlineKeyboardButton(
            text=f"⭐ {info['label']}",
            callback_data=f"stars_sub_select:{key}",
        )]
        for key, info in STARS_TARIFFS.items()
    ]

    await message.answer(text, parse_mode="Markdown",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ── callback: выбор тарифа → создать invoice ─────────────────────────────────

@subscription_stars_router.callback_query(F.data.startswith("stars_sub_select:"))
async def cb_stars_sub_select(callback: CallbackQuery):
    """Пользователь выбрал тариф — создаём Telegram Stars invoice."""
    await callback.answer()

    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    key = callback.data.split(":", 1)[1]
    tariff = STARS_TARIFFS.get(key)
    if not tariff:
        await callback.message.answer("Неизвестный тариф. Попробуйте /subscribe_stars.")
        return

    payload = f"stars_sub_{chat_id}_{key}"

    try:
        link = await callback.bot.create_invoice_link(
            title=f"Подписка БР — {tariff['label']}",
            description=(
                f"«Инженерия интеллекта» на {tariff['months']} мес. "
                "Доступ к полному курсу, Ленте, ЦД и Aisystant MCP."
            ),
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label="Подписка", amount=tariff["stars"])],
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"⭐ Оплатить {tariff['stars']} Stars",
                url=link,
            )],
            [InlineKeyboardButton(
                text="← Назад",
                callback_data="stars_sub_back",
            )],
        ])

        await callback.message.edit_text(
            f"⭐ *{tariff['label']}*\n\n"
            "Нажмите кнопку ниже для оплаты через Telegram Stars:",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"[SubStars] create_invoice_link error: {e}", exc_info=True)
        await callback.message.answer(
            "Не удалось создать ссылку на оплату. Попробуйте позже."
        )


@subscription_stars_router.callback_query(F.data == "stars_sub_back")
async def cb_stars_sub_back(callback: CallbackQuery):
    """Назад к выбору тарифа."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    buttons = [
        [InlineKeyboardButton(
            text=f"⭐ {info['label']}",
            callback_data=f"stars_sub_select:{key}",
        )]
        for key, info in STARS_TARIFFS.items()
    ]

    await callback.message.edit_text(
        "⭐ *Подписка «Инженерия интеллекта» через Telegram Stars*\n\n"
        "Выберите период:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ── Pre-checkout: обязательное подтверждение ─────────────────────────────────

@subscription_stars_router.pre_checkout_query(
    lambda q: q.invoice_payload.startswith("stars_sub_")
)
async def on_pre_checkout_stars_sub(pre_checkout_query: PreCheckoutQuery):
    """Подтверждаем Stars-подписку (≤10 сек)."""
    await pre_checkout_query.answer(ok=True)


# ── Successful payment: emit events ─────────────────────────────────────────

@subscription_stars_router.message(
    F.successful_payment,
    F.successful_payment.invoice_payload.startswith("stars_sub_"),
)
async def on_successful_stars_sub(message: Message):
    """Stars-подписка оплачена — emit payment_received + subscription_granted."""
    payment = message.successful_payment
    chat_id = message.chat.id
    payload_str = payment.invoice_payload  # "stars_sub_{chat_id}_{key}"

    # Парсим тариф из payload
    parts = payload_str.split("_")
    # "stars", "sub", chat_id, key — но key может быть "1m"/"3m"/... (без _)
    tariff_key = parts[-1] if len(parts) >= 4 else "1m"
    tariff = STARS_TARIFFS.get(tariff_key, STARS_TARIFFS["1m"])

    charge_id = payment.telegram_payment_charge_id
    stars_amount = payment.total_amount  # в Stars (XTR)
    months = tariff["months"]

    now_utc = datetime.now(timezone.utc)
    valid_to = now_utc + timedelta(days=30 * months)

    logger.info(
        f"[SubStars] Payment OK: chat_id={chat_id}, "
        f"tariff={tariff_key}, amount={stars_amount} XTR, "
        f"charge_id={charge_id}, valid_to={valid_to.isoformat()}"
    )

    # ── Resolve account_id (ory_id) — best-effort ──────────────────────────
    account_id = await resolve_ory_id_from_chat(chat_id)

    # ── 1. FSM-state: save_subscription в Railway Postgres (TTL≈24h) ───────
    # Q18: bot.subscriptions = FSM-only, permanent-state = subscription.contract
    try:
        await save_subscription(
            chat_id=chat_id,
            charge_id=charge_id,
            stars_amount=stars_amount,
            expires_at=valid_to.replace(tzinfo=None),  # naive UTC per bot convention
            is_first=True,
        )
    except Exception as e:
        logger.warning(f"[SubStars] save_subscription failed (non-blocking): {e}")

    # ── 2. Emit payment_received → payment.payment_received ─────────────────
    # projection-worker (WP-270) делает UPSERT через rule 104
    payment_event_id = str(uuid.uuid4())
    asyncio.create_task(post_event(
        source=EVENT_SOURCE,
        external_id=f"stars-sub-pay-{charge_id}",
        event_type="payment_received",
        schema_version="v1",
        occurred_at=now_utc,
        account_id=account_id,
        payload={
            "payment_id": payment_event_id,
            "amount": stars_amount,
            "currency": "XTR",
            "payment_kind_code": "stars",
            "external_payment_id": charge_id,
            "provider": "tg_stars",
            "paid_at": now_utc.isoformat(),
            "account_id_resolved": account_id,
            # telegram_id_lookup убран из payload (FORBIDDEN_FIELDS в gateway).
            # projection-worker делает lookup через persona.ory_identity.
        },
    ))

    # ── 3. Emit subscription_granted → subscription.contract ─────────────────
    # projection-worker (WP-270) делает UPSERT через rule 103.
    # external_id формат: "sub-granted-{telegram_id}-{source}-{valid_until_iso}"
    # (regex в rule 103: "^sub-granted-(\\d+)-" для извлечения telegram_id → account_id lookup)
    valid_until_iso = valid_to.isoformat()
    asyncio.create_task(post_event(
        source=EVENT_SOURCE,
        external_id=f"sub-granted-{chat_id}-tg_stars-{valid_until_iso}",
        event_type="subscription_granted",
        schema_version="v1",
        occurred_at=now_utc,
        account_id=account_id,
        payload={
            "product": tariff["name"],   # lookup: reference.tariffs WHERE name = this
            "source": "tg_stars",
            "valid_until": valid_until_iso,
            "mode": "created",
            "activating_payment_id": payment_event_id,
        },
    ))

    # ── 4. Ответ пользователю ────────────────────────────────────────────────
    await message.answer(
        f"✅ *Подписка активирована!*\n\n"
        f"Тариф: {tariff['label']}\n"
        f"Действует до: {valid_to.strftime('%d.%m.%Y')}\n\n"
        "Доступ к полному контенту появится через несколько секунд. "
        "Если тир не обновился — нажмите /start.",
        parse_mode="Markdown",
    )

    logger.info(
        f"[SubStars] Events emitted: payment_received+subscription_granted "
        f"chat_id={chat_id}, tariff={tariff_key}, valid_to={valid_to.isoformat()}"
    )
