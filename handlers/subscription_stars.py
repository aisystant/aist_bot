from __future__ import annotations

"""
Stars-подписки «Инженерия интеллекта» (WP-246 Ф1.1).

Поток:
  /subscribe_stars → выбор тарифа → invoice Stars →
  pre_checkout_query → successful_payment →
    одна транзакция: save subscription + outbox(payment_received) +
    outbox(subscription_granted) (WP-567 Ф3в) →
    отдельный крон-дожим (core/scheduler.py) отправляет outbox в event-gateway.

Архитектурные решения (Q16, Q18 WP-246):
- Handler пишет подписку в bot.subscriptions СИНХРОННО с outbox-событиями,
  одной транзакцией — не "FSM-only, TTL≈24h" (это устаревшее описание не
  соответствовало реальной схеме: `public.subscriptions` — постоянная
  запись, читаемая get_active_subscription/cancel_subscription, WP-567 Ф3в).
- Permanent projection-state (subscription.contract) — в Neon, доставляется
  через event-gateway + projection-worker, но САМА доставка теперь durable
  через локальный outbox, не fire-and-forget asyncio.create_task.
- source в event-gateway = "aist-bot" (уже в ALLOWED_SOURCES wrangler.toml).

Payload-формат invoice: "stars_sub_{chat_id}_{tariff_key}"
  tariff_key: "1m" | "3m" | "6m" | "12m"

# see DP.SC.120 (Payment Receiver), WP-246 Ф1.1, WP-567 Ф3(в)
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

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

from config import DEVELOPER_CHAT_ID
from db.queries import get_intern
from db.queries.subscription import save_subscription_with_outbox
from helpers.dual_write import resolve_ory_id_from_chat
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
            title="Подписка «Инженерия интеллекта»",
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
    valid_until_iso = valid_to.isoformat()

    logger.info(
        f"[SubStars] Payment OK: chat_id={chat_id}, "
        f"tariff={tariff_key}, amount={stars_amount} XTR, "
        f"charge_id={charge_id}, valid_to={valid_until_iso}"
    )

    # ── Resolve account_id (ory_id) — best-effort ──────────────────────────
    account_id = await resolve_ory_id_from_chat(chat_id)

    # ── Подписка + оба outbox-события — одна транзакция (WP-567 Ф3в) ───────
    # Раньше: save_subscription в try/except (проглатывала реальную потерю
    # записи подписки, не FSM-маркер, как называл этот комментарий раньше) +
    # два asyncio.create_task(post_event(...)) без ожидания результата
    # (событие терялось безвозвратно, если процесс убьют между созданием
    # таска и его выполнением). Теперь либо все три INSERT коммитятся вместе,
    # либо ни один.
    #
    # Ретраи, а не «raise и надейся на повтор от Telegram»: этот хендлер
    # выполняется В ФОНОВОЙ задаче aiogram (SimpleRequestHandler с
    # handle_in_background=True по умолчанию) — HTTP 200 Telegram уже ушёл
    # ДО того, как этот код начал выполняться, независимо от исхода. Telegram
    # не узнает об ошибке и не передоставит апдейт. Значит бюджет времени на
    # повтор здесь не ограничен таймаутом вебхука (проверено читкой
    # aiogram/webhook/aiohttp_server.py) — можно и нужно ретраить самим,
    # прежде чем сдаваться.
    payment_event_id = str(uuid.uuid4())
    payment_external_id = f"stars-sub-pay-{charge_id}"
    # regex в rule 103 projection-worker'а: "^sub-granted-(\d+)-" — формат
    # этого external_id менять нельзя, downstream парсинг на него завязан.
    # Suffix идентифицирует конкретный платёж (charge_id), не valid_until --
    # regex rule 103 парсит только "^sub-granted-(\d+)-" (chat_id), suffix
    # ей не важен, а charge_id делает external_id детерминированным при
    # повторном вызове хендлера для одного и того же платежа (в отличие от
    # valid_until_iso, вычисляемого заново из "now" при каждом вызове --
    # то давало бы разные строки на реальном повторе, срывая ON CONFLICT).
    granted_external_id = f"sub-granted-{chat_id}-tg_stars-{charge_id}"

    saved = False
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            await save_subscription_with_outbox(
                chat_id=chat_id,
                charge_id=charge_id,
                stars_amount=stars_amount,
                expires_at=valid_to.replace(tzinfo=None),  # naive UTC per bot convention
                account_id=account_id,
                payment_external_id=payment_external_id,
                payment_payload={
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
                granted_external_id=granted_external_id,
                granted_payload={
                    "product": tariff["name"],   # lookup: reference.tariffs WHERE name = this
                    "source": "tg_stars",
                    "valid_until": valid_until_iso,
                    "mode": "created",
                    "activating_payment_id": payment_event_id,
                },
                occurred_at=now_utc,
            )
            saved = True
            break
        except Exception as e:
            last_error = e
            logger.warning(f"[SubStars] save_subscription_with_outbox attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))

    if not saved:
        # Ни одна попытка не прошла — звёзды уже списаны у пользователя,
        # у нас нет способа получить повторную доставку от Telegram (см.
        # комментарий выше). Единственный оставшийся путь — алерт живому
        # человеку с деталями, достаточными для ручного восстановления, и
        # честный ответ пользователю (не молчание, не ложный "успех").
        logger.error(
            f"[SubStars] CRITICAL: subscription+outbox не сохранены после 3 попыток. "
            f"chat_id={chat_id}, charge_id={charge_id}, amount={stars_amount}, "
            f"tariff={tariff_key}, last_error={last_error}"
        )
        if DEVELOPER_CHAT_ID:
            try:
                await message.bot.send_message(
                    DEVELOPER_CHAT_ID,
                    f"🔴 Stars-подписка НЕ сохранена (3 попытки): "
                    f"chat_id={chat_id}, charge_id={charge_id}, "
                    f"amount={stars_amount} XTR, tariff={tariff_key}. "
                    f"Ошибка: {last_error}",
                )
            except Exception as alert_err:
                logger.error(f"[SubStars] dev-alert failed too: {alert_err}")
        await message.answer(
            "⚠️ Звёзды получены, но подписка обрабатывается дольше обычного. "
            "Если доступ не появится в течение нескольких минут — напишите в поддержку.",
        )
        return

    # Отправка в event-gateway — отдельный крон-дожим (core/scheduler.py
    # `_drain_event_outbox`), не этот хендлер. Строки уже закоммичены выше —
    # их доставка не блокирует ответ пользователю.

    # ── Ответ пользователю ────────────────────────────────────────────────
    await message.answer(
        f"✅ *Подписка активирована!*\n\n"
        f"Тариф: {tariff['label']}\n"
        f"Действует до: {valid_to.strftime('%d.%m.%Y')}\n\n"
        "Доступ к полному контенту появится через несколько секунд. "
        "Если тир не обновился — нажмите /start.",
        parse_mode="Markdown",
    )

    logger.info(
        f"[SubStars] Subscription + outbox committed "
        f"chat_id={chat_id}, tariff={tariff_key}, valid_to={valid_until_iso}"
    )
