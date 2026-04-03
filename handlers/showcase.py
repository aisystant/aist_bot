"""
Витрина семинаров (WP-5).

Каталог бесплатных и платных семинаров с оплатой через ЮКасса / Telegram Stars.
После оплаты → invite-ссылка в чат семинара.

Callbacks:
- showcase_main               — главное меню витрины
- showcase_free                — бесплатные семинары
- showcase_paid                — платные семинары
- showcase_detail:{id}         — карточка семинара
- showcase_pay_rub:{id}        — оплата рублями (ЮКасса)
- showcase_pay_stars:{id}      — оплата Stars
- showcase_check:{id}          — проверка оплаты
- showcase_back                — назад в витрину
"""

import logging
import os

from aiogram import Router, F, Bot
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from db.queries import get_intern
from db.queries.showcase import (
    get_active_seminars,
    get_seminar_by_id,
    get_seminar_by_code,
    create_seminar_payment,
    has_seminar_access,
    get_user_seminar_ids,
    increment_seminar_views,
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

    seminars = await get_active_seminars()
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
                callback_data=f"showcase_detail:{s['id']}",
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
    paid_ids = await get_user_seminar_ids(chat_id)

    lines = [t('showcase.paid_title', lang), ""]
    buttons = []

    for s in seminars:
        is_purchased = s["id"] in paid_ids
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
            callback_data=f"showcase_detail:{s['id']}",
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
    include_back: bool = True, views: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Построить текст + клавиатуру карточки семинара."""
    seminar_id = seminar["id"]

    lines = [f"*{seminar['title']}*", ""]
    lines.append(seminar['description'] or "")
    speaker = seminar.get('speaker') or ''
    meta_parts = []
    if speaker:
        meta_parts.append(speaker)
    meta_parts.append(seminar['duration'])
    if views > 0:
        meta_parts.append(t('showcase.views_count', lang, count=views))
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
            callback_data=f"showcase_pay_rub:{seminar_id}",
        )])
        if seminar['price_stars'] > 0:
            buttons.append([InlineKeyboardButton(
                text=t('showcase.btn_pay_stars', lang, stars=seminar['price_stars']),
                callback_data=f"showcase_pay_stars:{seminar_id}",
            )])

    # Кнопка «Поделиться»
    share_url = f"https://t.me/{BOT_USERNAME}?start=seminar_{seminar_id}"
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
    seminar_id = int(callback.data.split(":")[1])
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    seminar = await get_seminar_by_id(seminar_id)
    if not seminar:
        await callback.message.answer(t('showcase.not_found', lang))
        return

    is_purchased = await has_seminar_access(chat_id, seminar_id)
    views = await increment_seminar_views(seminar_id)
    text, keyboard = _build_seminar_card(seminar, lang, is_purchased, views=views)
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


async def _show_seminar_card(message, seminar_id: int):
    """Показать карточку семинара по deep link (вызывается из onboarding.py)."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    seminar = await get_seminar_by_id(seminar_id)
    if not seminar:
        await message.answer(t('showcase.not_found', lang))
        return

    is_purchased = await has_seminar_access(chat_id, seminar_id)
    views = await increment_seminar_views(seminar_id)
    text, keyboard = _build_seminar_card(seminar, lang, is_purchased, include_back=False, views=views)
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Оплата рублями (ЮКасса) ──────────────────────────


@showcase_router.callback_query(F.data.startswith("showcase_pay_rub:"))
async def callback_pay_rub(callback: CallbackQuery):
    """Оплата семинара рублями через ЮКасса."""
    seminar_id = int(callback.data.split(":")[1])
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    seminar = await get_seminar_by_id(seminar_id)
    if not seminar:
        await callback.message.answer(t('showcase.not_found', lang))
        return

    yk = _get_yookassa()
    if not yk:
        logger.error(f"[Showcase] YooKassa not configured, tg={chat_id}")
        await callback.message.answer(t('showcase.pay_error', lang))
        return

    try:
        result = await yk.create_payment(
            amount=seminar["price_rub"],
            description=seminar["title"],
            return_url="https://t.me/aist_me_bot",
            metadata={
                "telegram_id": str(chat_id),
                "purpose": "SEMINAR",
                "seminar_id": str(seminar_id),
            },
        )
        confirmation_url = result["confirmation_url"]
        logger.info(f"[Showcase] yookassa payment: tg={chat_id}, seminar={seminar_id}, payment_id={result['id']}")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('showcase.btn_pay_rub', lang, amount=seminar["price_rub"]),
                url=confirmation_url,
            )],
            [InlineKeyboardButton(
                text=t('showcase.btn_paid_check', lang),
                callback_data=f"showcase_check:{seminar_id}",
            )],
            [InlineKeyboardButton(
                text=t('showcase.btn_back_showcase', lang), callback_data="showcase_main",
            )],
        ])
        await callback.message.answer(
            t('showcase.pay_redirect', lang),
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"[Showcase] yookassa error: tg={chat_id}, seminar={seminar_id}, error={e}")
        await callback.message.answer(t('showcase.pay_error', lang))


# ── Оплата Stars ──────────────────────────────────────


@showcase_router.callback_query(F.data.startswith("showcase_pay_stars:"))
async def callback_pay_stars(callback: CallbackQuery):
    """Оплата семинара через Telegram Stars."""
    seminar_id = int(callback.data.split(":")[1])
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    seminar = await get_seminar_by_id(seminar_id)
    if not seminar:
        await callback.message.answer(t('showcase.not_found', lang))
        return

    try:
        link = await callback.bot.create_invoice_link(
            title=seminar["title"],
            description=seminar["description"] or seminar["title"],
            payload=f"seminar_{seminar_id}_{chat_id}",
            currency="XTR",
            prices=[LabeledPrice(label=seminar["title"], amount=seminar["price_stars"])],
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('showcase.btn_pay_stars', lang, stars=seminar["price_stars"]),
                url=link,
            )],
            [InlineKeyboardButton(
                text=t('showcase.btn_back_showcase', lang), callback_data="showcase_main",
            )],
        ])
        await callback.message.answer(
            t('showcase.pay_stars_intro', lang, title=seminar["title"], stars=seminar["price_stars"]),
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"[Showcase] stars error: tg={chat_id}, seminar={seminar_id}, error={e}")
        await callback.message.answer(t('showcase.pay_error', lang))


# ── Проверка оплаты ───────────────────────────────────


@showcase_router.callback_query(F.data.startswith("showcase_check:"))
async def callback_check(callback: CallbackQuery):
    """Проверка оплаты семинара (после ЮКасса redirect)."""
    seminar_id = int(callback.data.split(":")[1])
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    is_purchased = await has_seminar_access(chat_id, seminar_id)

    if not is_purchased:
        await callback.answer(t('showcase.check_not_yet', lang), show_alert=True)
        return

    await callback.answer()

    seminar = await get_seminar_by_id(seminar_id)
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
    """Успешная оплата семинара (Stars) → записать → выдать доступ."""
    payment = message.successful_payment
    payload = getattr(payment, 'invoice_payload', '') or ''

    if not payload.startswith("seminar_"):
        return

    # payload: seminar_{id}_{chat_id}
    parts = payload.split("_")
    if len(parts) < 3:
        logger.error(f"[Showcase] bad payload: {payload}")
        return

    seminar_id = int(parts[1])
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    charge_id = payment.telegram_payment_charge_id

    await create_seminar_payment(
        telegram_id=chat_id,
        seminar_id=seminar_id,
        amount=payment.total_amount,
        currency="XTR",
        source="stars",
        payment_id=charge_id,
    )

    logger.info(f"[Showcase] stars payment recorded: tg={chat_id}, seminar={seminar_id}")

    seminar = await get_seminar_by_id(seminar_id)
    if seminar:
        await _send_seminar_access(message.bot, chat_id, seminar, lang, message)


# ── Выдача доступа ────────────────────────────────────


async def _send_seminar_access(bot: Bot, chat_id: int, seminar: dict, lang: str, message=None):
    """Отправить ссылку на видео + invite в чат семинара (если есть chat_id)."""
    parts = [t('showcase.payment_success', lang, title=seminar["title"])]

    if seminar.get("video_url"):
        parts.append(t('showcase.video_link', lang, url=seminar["video_url"]))

    if seminar.get("chat_id"):
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=seminar["chat_id"],
                creates_join_request=True,
            )
            parts.append(t('showcase.chat_link', lang, url=invite.invite_link))
        except Exception as e:
            logger.error(f"[Showcase] invite error: seminar={seminar['id']}, chat={seminar['chat_id']}, error={e}")
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
    seminar_id = metadata.get("seminar_id")

    if not telegram_id or not seminar_id:
        return {"ok": False, "error": "missing telegram_id or seminar_id"}

    telegram_id = int(telegram_id)
    seminar_id = int(seminar_id)

    amount_obj = payment_obj.get("amount", {})
    amount = int(float(amount_obj.get("value", "0")))

    row_id = await create_seminar_payment(
        telegram_id=telegram_id,
        seminar_id=seminar_id,
        amount=amount,
        currency="RUB",
        source="yookassa",
        payment_id=payment_id,
    )

    if row_id == 0:
        return {"ok": True, "duplicate": True}

    seminar = await get_seminar_by_id(seminar_id)
    if seminar:
        intern = await get_intern(telegram_id)
        lang = _lang(intern)
        await _send_seminar_access(bot, telegram_id, seminar, lang)

    logger.info(f"[Showcase] yookassa webhook: tg={telegram_id}, seminar={seminar_id}, amount={amount}")
    return {"ok": True, "seminar_id": seminar_id, "payment_row_id": row_id}


# ── Webhook от Aisystant/Tilda для семинаров ──────────


async def process_seminar_aisystant_webhook(data: dict, bot: Bot) -> dict:
    """Обработать webhook от Aisystant при оплате семинара.

    Вызывается из oauth_server.py → POST /webhook/workshop-payment
    при purpose == "SEMINAR".

    Body: {"telegram_id": 123, "seminar_id": 3, "amount": 5000,
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

    # Определяем seminar_id: напрямую или по коду (tilda_uid)
    seminar_id = data.get("seminar_id")
    seminar_code = data.get("seminar_code")

    if seminar_id:
        seminar_id = int(seminar_id)
        seminar = await get_seminar_by_id(seminar_id)
    elif seminar_code:
        seminar = await get_seminar_by_code(seminar_code)
        seminar_id = seminar["id"] if seminar else None
    else:
        return {"ok": False, "error": "missing seminar_id or seminar_code"}

    if not seminar:
        return {"ok": False, "error": f"seminar not found: id={seminar_id}, code={seminar_code}"}

    row_id = await create_seminar_payment(
        telegram_id=telegram_id,
        seminar_id=seminar_id,
        amount=amount,
        currency="RUB",
        source="aisystant_webhook",
        payment_id=payment_id,
    )

    if row_id == 0:
        return {"ok": True, "duplicate": True}

    # Отправляем доступ
    intern = await get_intern(telegram_id)
    lang = _lang(intern)
    await _send_seminar_access(bot, telegram_id, seminar, lang)

    logger.info(f"[Showcase] aisystant webhook: tg={telegram_id}, seminar={seminar_id}, amount={amount}")
    return {"ok": True, "seminar_id": seminar_id, "payment_row_id": row_id}
