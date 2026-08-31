from __future__ import annotations

"""
Витрина семинаров (WP-5).

Каталог бесплатных и платных семинаров с оплатой через ЮКасса / Telegram Stars.
После оплаты → invite-ссылка в чат семинара.

Данные: таблица products (type='seminar'), оплата → finance_payments.

Callbacks:
- showcase_main                — главное меню витрины
- showcase_free                — бесплатные семинары
- showcase_paid                — платные семинары
- showcase_detail:{code}       — карточка семинара
- showcase_pay_rub:{code}      — оплата рублями (ЮКасса)
- showcase_pay_stars:{code}    — оплата Stars
- showcase_check:{code}        — проверка оплаты
- showcase_back                — назад в витрину
"""

import logging
import os

from aiogram import Router, F, Bot
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from db.queries import get_intern
from db.queries.showcase import (
    get_active_seminars,
    get_seminar_by_code,
    get_seminar_by_tilda_uid,
    create_seminar_payment,
    has_seminar_access,
    get_user_seminar_codes,
)
from db.queries.redeem import confirm_burn
from helpers.redeem_helpers import (
    prepare_burn_offer,
    format_burn_offer_text,
    build_burn_offer_keyboard,
    reserve_for_yookassa,
    reserve_for_tg_stars,
    discount_stars,
)
from clients.yookassa import YooKassaClient
from i18n import t

logger = logging.getLogger(__name__)

showcase_router = Router(name="showcase")

# ── Config ─────────────────────────────────────────────

# ЮКасса — переиспользуем тот же магазин из workshop.py
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
_yookassa_client: YooKassaClient | None = None


def _get_yookassa() -> YooKassaClient | None:
    global _yookassa_client
    if _yookassa_client is None and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
        _yookassa_client = YooKassaClient(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    return _yookassa_client


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


# ── Главное меню витрины ──────────────────────────────


@showcase_router.callback_query(F.data == "showcase_main")
async def callback_showcase_main(callback: CallbackQuery):
    """Витрина семинаров — главное меню с двумя разделами."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    try:
        seminars = await get_active_seminars()
    except Exception as e:
        logger.error(f"[Showcase] get_active_seminars error: {e}")
        await callback.message.answer(t('showcase.error', lang))
        return

    free_count = sum(1 for s in seminars if s["is_free"])
    paid_count = sum(1 for s in seminars if not s["is_free"])

    text = t('showcase.main_title', lang)

    buttons = []
    if free_count > 0:
        buttons.append([InlineKeyboardButton(
            text=t('showcase.btn_free', lang, count=free_count),
            callback_data="showcase_free",
        )])
    if paid_count > 0:
        buttons.append([InlineKeyboardButton(
            text=t('showcase.btn_paid', lang, count=paid_count),
            callback_data="showcase_paid",
        )])

    # Кнопка «Назад» в расписание
    buttons.append([InlineKeyboardButton(
        text=t('schedule.btn_back', lang), callback_data="sched_back",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Бесплатные семинары ───────────────────────────────


@showcase_router.callback_query(F.data == "showcase_free")
async def callback_showcase_free(callback: CallbackQuery):
    """Список бесплатных семинаров — сразу с кнопками на видео."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    seminars = await get_active_seminars(free_only=True)

    lines = [t('showcase.free_title', lang), ""]
    buttons = []

    for s in seminars:
        lines.append(f"*{s['title']}*")
        lines.append(f"{s['description']}")
        speaker = s.get('speaker') or ''
        lines.append(f"_{speaker}, {s['duration']}_" if speaker else f"_{s['duration']}_")
        lines.append("")

        if s.get("video_url"):
            buttons.append([InlineKeyboardButton(
                text=f"🎬 {s['title'][:40]}",
                url=s["video_url"],
            )])
        else:
            buttons.append([InlineKeyboardButton(
                text=f"📋 {s['title'][:40]}",
                callback_data=f"showcase_detail:{s['code']}",
            )])

    buttons.append([InlineKeyboardButton(
        text=t('showcase.btn_back_showcase', lang), callback_data="showcase_main",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


# ── Платные семинары ──────────────────────────────────


@showcase_router.callback_query(F.data == "showcase_paid")
async def callback_showcase_paid(callback: CallbackQuery):
    """Список платных семинаров — карточки с ценами."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    seminars = [s for s in await get_active_seminars() if not s["is_free"]]
    paid_codes = await get_user_seminar_codes(chat_id)

    lines = [t('showcase.paid_title', lang), ""]
    buttons = []

    for s in seminars:
        is_purchased = s["code"] in paid_codes
        status = "✅" if is_purchased else f"{s['price_rub']}₽"
        speaker = s.get('speaker') or ''
        lines.append(f"*{s['title']}*")
        lines.append(f"{s['description']}")
        lines.append(f"_{speaker}, {s['duration']}_ | {status}" if speaker else f"_{s['duration']}_ | {status}")
        lines.append("")

        if is_purchased:
            label = f"✅ {s['title'][:35]}"
        else:
            label = f"💳 {s['title'][:30]} — {s['price_rub']}₽"

        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"showcase_detail:{s['code']}",
        )])

    buttons.append([InlineKeyboardButton(
        text=t('showcase.btn_back_showcase', lang), callback_data="showcase_main",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


# ── Карточка семинара ─────────────────────────────────


BOT_USERNAME = os.getenv("BOT_USERNAME", "aist_me_bot")


def _build_seminar_card(
    seminar: dict, lang: str, is_purchased: bool, *,
    include_back: bool = True,
) -> tuple[str, InlineKeyboardMarkup]:
    """Построить текст + клавиатуру карточки семинара."""
    code = seminar["code"]

    lines = [f"*{seminar['title']}*", ""]
    lines.append(seminar['description'] or "")
    speaker = seminar.get('speaker') or ''
    meta_parts = []
    if speaker:
        meta_parts.append(speaker)
    if seminar.get('duration'):
        meta_parts.append(seminar['duration'])
    if meta_parts:
        lines.append(f"\n_{', '.join(meta_parts)}_")

    buttons = []

    if seminar["is_free"]:
        if seminar.get("video_url"):
            buttons.append([InlineKeyboardButton(
                text=t('showcase.btn_watch', lang),
                url=seminar["video_url"],
            )])
        lines.append(f"\n{t('showcase.free_label', lang)}")

    elif is_purchased:
        lines.append(f"\n{t('showcase.already_purchased', lang)}")
        if seminar.get("video_url"):
            buttons.append([InlineKeyboardButton(
                text=t('showcase.btn_watch', lang),
                url=seminar["video_url"],
            )])

    else:
        lines.append(f"\n{t('showcase.price_label', lang, rub=seminar['price_rub'], stars=seminar['price_stars'])}")
        buttons.append([InlineKeyboardButton(
            text=t('showcase.btn_pay_rub', lang, amount=seminar['price_rub']),
            callback_data=f"showcase_pay_rub:{code}",
        )])
        if seminar['price_stars'] > 0:
            buttons.append([InlineKeyboardButton(
                text=t('showcase.btn_pay_stars', lang, stars=seminar['price_stars']),
                callback_data=f"showcase_pay_stars:{code}",
            )])

    # Кнопка «Поделиться»
    share_url = f"https://t.me/{BOT_USERNAME}?start=seminar_{code}"
    buttons.append([InlineKeyboardButton(
        text=t('showcase.btn_share', lang),
        url=f"https://t.me/share/url?url={share_url}&text={seminar['title']}",
    )])

    if include_back:
        buttons.append([InlineKeyboardButton(
            text=t('showcase.btn_back_showcase', lang),
            callback_data="showcase_paid" if not seminar["is_free"] else "showcase_free",
        )])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@showcase_router.callback_query(F.data.startswith("showcase_detail:"))
async def callback_showcase_detail(callback: CallbackQuery):
    """Карточка конкретного семинара: описание + кнопки покупки/просмотра."""
    code = callback.data.split(":", 1)[1]
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    seminar = await get_seminar_by_code(code)
    if not seminar:
        await callback.message.answer(t('showcase.not_found', lang))
        return

    is_purchased = await has_seminar_access(chat_id, code)
    text, keyboard = _build_seminar_card(seminar, lang, is_purchased)
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


async def _show_seminar_card(message, seminar_code: str):
    """Показать карточку семинара по deep link (вызывается из onboarding.py)."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    seminar = await get_seminar_by_code(seminar_code)
    if not seminar:
        await message.answer(t('showcase.not_found', lang))
        return

    is_purchased = await has_seminar_access(chat_id, seminar_code)
    text, keyboard = _build_seminar_card(seminar, lang, is_purchased, include_back=False)
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Оплата рублями (ЮКасса) ──────────────────────────


@showcase_router.callback_query(F.data.startswith("showcase_pay_rub:"))
async def callback_pay_rub(callback: CallbackQuery):
    """Оплата семинара рублями. Если есть баллы — предложить скидку (WP-327)."""
    code = callback.data.split(":", 1)[1]
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)
    await callback.answer()

    seminar = await get_seminar_by_code(code)
    if not seminar:
        await callback.message.answer(t('showcase.not_found', lang))
        return

    burn = await prepare_burn_offer(chat_id, seminar["price_rub"], skip_ceiling=True)
    if burn is not None:
        text = format_burn_offer_text(burn, item_title=f"«{seminar['title']}»")
        kb = build_burn_offer_keyboard(
            apply_data=f"showcase_pay_rub_burn:{code}",
            skip_data=f"showcase_pay_rub_full:{code}",
            full_amount_rub=seminar["price_rub"],
            back_data="showcase_main",
        )
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    await _pay_yookassa_seminar(callback, chat_id, lang, seminar, apply_burn=False)


@showcase_router.callback_query(F.data.startswith("showcase_pay_rub_full:"))
async def callback_pay_rub_full(callback: CallbackQuery):
    """Оплата семинара рублями без применения баллов."""
    code = callback.data.split(":", 1)[1]
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)
    await callback.answer()
    seminar = await get_seminar_by_code(code)
    if not seminar:
        await callback.message.answer(t('showcase.not_found', lang))
        return
    await _pay_yookassa_seminar(callback, chat_id, lang, seminar, apply_burn=False)


@showcase_router.callback_query(F.data.startswith("showcase_pay_rub_burn:"))
async def callback_pay_rub_burn(callback: CallbackQuery):
    """Оплата семинара рублями с применением скидки баллами (WP-327)."""
    code = callback.data.split(":", 1)[1]
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)
    await callback.answer()
    seminar = await get_seminar_by_code(code)
    if not seminar:
        await callback.message.answer(t('showcase.not_found', lang))
        return
    await _pay_yookassa_seminar(callback, chat_id, lang, seminar, apply_burn=True)


async def _pay_yookassa_seminar(
    callback: CallbackQuery, chat_id: int, lang: str, seminar: dict, apply_burn: bool
):
    """Унифицированный flow создания YK-платежа для семинара витрины."""
    yk = _get_yookassa()
    if not yk:
        logger.error(f"[Showcase] YooKassa not configured, tg={chat_id}")
        await callback.message.answer(t('showcase.pay_error', lang))
        return

    code = seminar["code"]
    full_amount = seminar["price_rub"]
    burn = await prepare_burn_offer(chat_id, full_amount, skip_ceiling=True) if apply_burn else None
    payable_rub = int(burn["payable_rub"]) if burn else full_amount

    metadata = {"telegram_id": str(chat_id), "purpose": "SEMINAR", "product_code": code}
    if burn:
        metadata["points_amount"] = str(burn["available_pts"])
        metadata["account_id"] = burn["account_id"]

    try:
        result = await yk.create_payment(
            amount=payable_rub,
            description=seminar["title"],
            return_url="https://t.me/aist_me_bot",
            metadata=metadata,
        )
        payment_id = result["id"]
        confirmation_url = result["confirmation_url"]
        logger.info(f"[Showcase] yookassa payment: tg={chat_id}, product={code}, payment_id={payment_id}, amount={payable_rub}, burn={'yes' if burn else 'no'}")

        applied_discount_rub = 0
        if burn:
            ok, points_used = await reserve_for_yookassa(burn, payment_id, purpose="SEMINAR")
            if ok:
                applied_discount_rub = int(burn["discount_rub"])
                logger.info(f"[Redeem] Showcase reserve OK: tg={chat_id}, payment_id={payment_id}, points={points_used}")
            else:
                logger.warning(f"[Redeem] Showcase reserve failed (race), recreating full price: tg={chat_id}")
                result = await yk.create_payment(
                    amount=full_amount,
                    description=seminar["title"],
                    return_url="https://t.me/aist_me_bot",
                    metadata={"telegram_id": str(chat_id), "purpose": "SEMINAR", "product_code": code},
                )
                payment_id = result["id"]
                confirmation_url = result["confirmation_url"]
                payable_rub = full_amount

        extra = f"\n\nПрименена скидка {applied_discount_rub} ₽ из бонусов." if applied_discount_rub > 0 else ""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('showcase.btn_pay_rub', lang, amount=payable_rub), url=confirmation_url)],
            [InlineKeyboardButton(text=t('showcase.btn_paid_check', lang), callback_data=f"showcase_check:{code}")],
            [InlineKeyboardButton(text=t('showcase.btn_back_showcase', lang), callback_data="showcase_main")],
        ])
        await callback.message.answer(
            t('showcase.pay_redirect', lang) + extra,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"[Showcase] yookassa error: tg={chat_id}, product={code}, error={e}")
        await callback.message.answer(t('showcase.pay_error', lang))


# ── Оплата Stars ──────────────────────────────────────


@showcase_router.callback_query(F.data.startswith("showcase_pay_stars:"))
async def callback_pay_stars(callback: CallbackQuery):
    """Оплата семинара через Telegram Stars. Если есть баллы — предложить скидку (WP-327)."""
    code = callback.data.split(":", 1)[1]
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)
    await callback.answer()

    seminar = await get_seminar_by_code(code)
    if not seminar:
        await callback.message.answer(t('showcase.not_found', lang))
        return

    burn = await prepare_burn_offer(chat_id, seminar["price_rub"], skip_ceiling=True)
    if burn is not None:
        text = format_burn_offer_text(burn, item_title=f"«{seminar['title']}» (Stars)")
        kb = build_burn_offer_keyboard(
            apply_data=f"showcase_pay_stars_burn:{code}",
            skip_data=f"showcase_pay_stars_full:{code}",
            full_amount_rub=seminar["price_rub"],
            back_data="showcase_main",
        )
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    await _pay_stars_seminar(callback, chat_id, lang, seminar, apply_burn=False)


@showcase_router.callback_query(F.data.startswith("showcase_pay_stars_full:"))
async def callback_pay_stars_full(callback: CallbackQuery):
    """Оплата Stars без применения баллов."""
    code = callback.data.split(":", 1)[1]
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)
    await callback.answer()
    seminar = await get_seminar_by_code(code)
    if not seminar:
        return
    await _pay_stars_seminar(callback, chat_id, lang, seminar, apply_burn=False)


@showcase_router.callback_query(F.data.startswith("showcase_pay_stars_burn:"))
async def callback_pay_stars_burn(callback: CallbackQuery):
    """Оплата Stars с применением скидки баллами (WP-327)."""
    code = callback.data.split(":", 1)[1]
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)
    await callback.answer()
    seminar = await get_seminar_by_code(code)
    if not seminar:
        return
    await _pay_stars_seminar(callback, chat_id, lang, seminar, apply_burn=True)


async def _pay_stars_seminar(
    callback: CallbackQuery, chat_id: int, lang: str, seminar: dict, apply_burn: bool
):
    """Унифицированный flow Stars-оплаты семинара витрины."""
    code = seminar["code"]
    burn = await prepare_burn_offer(chat_id, seminar["price_rub"], skip_ceiling=True) if apply_burn else None
    provisional_id = None
    payable_stars = seminar["price_stars"]
    applied_discount_rub = 0

    if burn:
        provisional_id, _ = await reserve_for_tg_stars(burn, purpose="SEMINAR")
        if provisional_id:
            payable_stars = discount_stars(burn, seminar["price_stars"], seminar["price_rub"])
            applied_discount_rub = int(burn["discount_rub"])
            logger.info(f"[Redeem] Showcase Stars reserve OK: tg={chat_id}, provisional={provisional_id}, payable_stars={payable_stars}")
        else:
            logger.warning(f"[Redeem] Showcase Stars reserve failed (race): tg={chat_id}")

    payload = f"seminar_{code}_{chat_id}"
    if provisional_id:
        payload = f"{payload}_p_{provisional_id}"

    try:
        link = await callback.bot.create_invoice_link(
            title=seminar["title"],
            description=seminar["description"] or seminar["title"],
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=seminar["title"], amount=payable_stars)],
        )
        extra = f"\n\nПрименена скидка {applied_discount_rub} ₽ из бонусов." if applied_discount_rub > 0 else ""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('showcase.btn_pay_stars', lang, stars=payable_stars), url=link)],
            [InlineKeyboardButton(text=t('showcase.btn_back_showcase', lang), callback_data="showcase_main")],
        ])
        await callback.message.answer(
            t('showcase.pay_stars_intro', lang, title=seminar["title"], stars=payable_stars) + extra,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"[Showcase] stars error: tg={chat_id}, product={code}, error={e}")
        await callback.message.answer(t('showcase.pay_error', lang))


# ── Проверка оплаты ───────────────────────────────────


@showcase_router.callback_query(F.data.startswith("showcase_check:"))
async def callback_check(callback: CallbackQuery):
    """Проверка оплаты семинара (после ЮКасса redirect)."""
    code = callback.data.split(":", 1)[1]
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    is_purchased = await has_seminar_access(chat_id, code)

    if not is_purchased:
        await callback.answer(t('showcase.check_not_yet', lang), show_alert=True)
        return

    await callback.answer()

    seminar = await get_seminar_by_code(code)
    if not seminar:
        return

    await _send_seminar_access(callback.bot, chat_id, seminar, lang, callback.message)


# ── Payment handlers (Stars) ─────────────────────────


@showcase_router.pre_checkout_query(lambda q: q.invoice_payload.startswith("seminar_"))
async def on_seminar_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    """Подтверждение платежа за семинар (Stars)."""
    logger.info(f"[Showcase] pre_checkout: tg={pre_checkout_query.from_user.id}, payload={pre_checkout_query.invoice_payload}")
    await pre_checkout_query.answer(ok=True)


@showcase_router.message(F.successful_payment)
async def on_seminar_payment(message: Message):
    """Успешная оплата семинара (Stars) → записать → выдать доступ. WP-327: confirm_burn если был резерв."""
    payment = message.successful_payment
    payload = getattr(payment, 'invoice_payload', '') or ''

    if not payload.startswith("seminar_"):
        return

    # WP-327: provisional_id для burn (если был) — в суффиксе после _p_
    provisional_id: str | None = None
    if "_p_" in payload:
        payload_main, provisional_id = payload.rsplit("_p_", 1)
    else:
        payload_main = payload

    # payload_main: seminar_{code}_{chat_id}
    parts = payload_main.split("_")
    if len(parts) < 3:
        logger.error(f"[Showcase] bad payload: {payload}")
        return

    chat_id = message.chat.id
    code = "_".join(parts[1:-1])
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    charge_id = payment.telegram_payment_charge_id

    await create_seminar_payment(
        telegram_id=chat_id,
        product_code=code,
        amount=payment.total_amount,
        currency="XTR",
        source="stars",
        payment_id=charge_id,
    )

    logger.info(f"[Showcase] stars payment recorded: tg={chat_id}, product={code}")

    # WP-266 Ф5c: сырой payment_received (welcome/referral решает воркер)
    try:
        from helpers.dual_write import emit_payment_received
        await emit_payment_received(
            provider="tg_stars",
            external_payment_id=charge_id,
            amount=payment.total_amount,
            currency="XTR",
            payment_kind_code="stars",
            telegram_id=chat_id,
        )
    except Exception as e:
        logger.error(f"[payment-event] seminar stars emit failed for tg={chat_id}: {e}")

    # WP-327: confirm_burn по provisional_id
    if provisional_id:
        try:
            ok = await confirm_burn(provisional_id)
            logger.info(f"[Redeem] Showcase Stars confirm_burn(provisional={provisional_id}) result={ok}, tg={chat_id}")
        except Exception as e:
            logger.error(f"[Redeem] Showcase Stars confirm_burn exception: provisional={provisional_id}, tg={chat_id}, error={e}")

    seminar = await get_seminar_by_code(code)
    if seminar:
        await _send_seminar_access(message.bot, chat_id, seminar, lang, message)


# ── Выдача доступа ────────────────────────────────────


async def _send_seminar_access(bot: Bot, chat_id: int, seminar: dict, lang: str, message=None):
    """Отправить ссылку на видео + invite в чат семинара (если есть tg_chat_id)."""
    parts = [t('showcase.payment_success', lang, title=seminar["title"])]

    if seminar.get("video_url"):
        parts.append(t('showcase.video_link', lang, url=seminar["video_url"]))

    if seminar.get("tg_chat_id"):
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=seminar["tg_chat_id"],
                creates_join_request=True,
            )
            parts.append(t('showcase.chat_link', lang, url=invite.invite_link))
        except Exception as e:
            logger.error(f"[Showcase] invite error: product={seminar['code']}, chat={seminar['tg_chat_id']}, error={e}")
            parts.append(t('showcase.invite_error', lang))

    text = "\n\n".join(parts)

    if message:
        await message.answer(text, disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id, text, disable_web_page_preview=True)


# ── Webhook от ЮКасса для семинаров ──────────────────


async def process_seminar_yookassa_webhook(data: dict, bot: Bot) -> dict:
    """Обработать webhook от ЮКасса для оплаты семинара.

    Вызывается из oauth_server.py (проверяется metadata.purpose == "SEMINAR").
    """
    event_type = data.get("event", "")
    payment_obj = data.get("object", {})
    payment_id = payment_obj.get("id", "")

    if event_type != "payment.succeeded":
        return {"ok": True, "skipped": event_type}

    metadata = payment_obj.get("metadata", {})
    telegram_id = metadata.get("telegram_id")
    product_code = metadata.get("product_code")

    if not telegram_id or not product_code:
        return {"ok": False, "error": "missing telegram_id or product_code"}

    telegram_id = int(telegram_id)

    amount_obj = payment_obj.get("amount", {})
    amount = int(float(amount_obj.get("value", "0")))

    row_id = await create_seminar_payment(
        telegram_id=telegram_id,
        product_code=product_code,
        amount=amount,
        currency="RUB",
        source="yookassa",
        payment_id=payment_id,
    )

    if row_id == 0:
        # Идемпотентность: на дубле тоже пытаемся confirm_burn (no-op если уже confirmed)
        try:
            await confirm_burn(payment_id)
        except Exception as e:
            logger.error(f"[Redeem] Showcase confirm_burn on duplicate: payment_id={payment_id}, error={e}")
            raise
        return {"ok": True, "duplicate": True}

    # WP-327: подтвердить burn по реальному YK payment_id (no-op если резерва не было)
    try:
        ok = await confirm_burn(payment_id)
        logger.info(f"[Redeem] Showcase confirm_burn(yk={payment_id}) result={ok}")
    except Exception as e:
        logger.error(f"[Redeem] Showcase confirm_burn exception: payment_id={payment_id}, error={e}")
        raise

    # WP-266 Ф5c: сырой payment_received (welcome/referral решает воркер)
    try:
        from helpers.dual_write import emit_payment_received
        await emit_payment_received(
            provider="yookassa",
            external_payment_id=payment_id,
            amount=amount,
            currency="RUB",
            payment_kind_code="bank_card",
            telegram_id=telegram_id,
        )
    except Exception as e:
        logger.error(f"[payment-event] seminar yookassa emit failed for tg={telegram_id}: {e}")

    seminar = await get_seminar_by_code(product_code)
    if seminar:
        intern = await get_intern(telegram_id)
        lang = _lang(intern)
        await _send_seminar_access(bot, telegram_id, seminar, lang)

    logger.info(f"[Showcase] yookassa webhook: tg={telegram_id}, product={product_code}, amount={amount}")
    return {"ok": True, "product_code": product_code, "payment_row_id": row_id}


# ── Webhook от Aisystant/Tilda для семинаров ──────────


async def process_seminar_aisystant_webhook(data: dict, bot: Bot) -> dict:
    """Обработать webhook от Aisystant при оплате семинара.

    Вызывается из oauth_server.py → POST /webhook/workshop-payment
    при purpose == "SEMINAR".

    Body: {"telegram_id": 123, "product_code": "SE-2026.2-T-sem", "amount": 5000,
           "payment_id": "...", "purpose": "SEMINAR"}

    Также поддерживает поиск по tilda_uid:
    {"telegram_id": 123, "seminar_code": "SE-2026.2-T", "amount": 5000, "purpose": "SEMINAR"}
    """
    telegram_id = data.get("telegram_id")
    if not telegram_id:
        return {"ok": False, "error": "missing telegram_id"}

    telegram_id = int(telegram_id)
    payment_id = data.get("payment_id")
    amount = data.get("amount", 0)

    # Определяем product_code: напрямую или по tilda_uid
    product_code = data.get("product_code")
    seminar_code = data.get("seminar_code")

    if product_code:
        seminar = await get_seminar_by_code(product_code)
    elif seminar_code:
        seminar = await get_seminar_by_tilda_uid(seminar_code)
        product_code = seminar["code"] if seminar else None
    else:
        return {"ok": False, "error": "missing product_code or seminar_code"}

    if not seminar:
        return {"ok": False, "error": f"seminar not found: code={product_code}, tilda_uid={seminar_code}"}

    row_id = await create_seminar_payment(
        telegram_id=telegram_id,
        product_code=product_code,
        amount=amount,
        currency="RUB",
        source="aisystant_webhook",
        payment_id=payment_id,
    )

    if row_id == 0:
        return {"ok": True, "duplicate": True}

    # WP-266 Ф5c: сырой payment_received (welcome/referral решает воркер).
    # Без payment_id helper пропустит эмиссию с warning.
    try:
        from helpers.dual_write import emit_payment_received
        await emit_payment_received(
            provider="aisystant",
            external_payment_id=payment_id,
            amount=amount,
            currency="RUB",
            payment_kind_code="manual",
            telegram_id=telegram_id,
        )
    except Exception as e:
        logger.error(f"[payment-event] seminar aisystant emit failed for tg={telegram_id}: {e}")

    # Отправляем доступ
    intern = await get_intern(telegram_id)
    lang = _lang(intern)
    await _send_seminar_access(bot, telegram_id, seminar, lang)

    logger.info(f"[Showcase] aisystant webhook: tg={telegram_id}, product={product_code}, amount={amount}")
    return {"ok": True, "product_code": product_code, "payment_row_id": row_id}
