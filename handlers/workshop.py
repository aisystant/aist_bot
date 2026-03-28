"""
Сообщество IWE — допуск через оплату семинара (WP-181).

Callbacks:
- sched_seminar_iwe        — меню «Семинар IWE» (по count оплат)
- seminar_iwe_pay          — оплата семинара (5000₽)
- chat_join_request        — одобрение заявки на вход в чат

Также:
- ChatMemberUpdated        — логирование вступлений/выходов
- /community_report        — admin-отчёт по чатам
"""

import logging
import os

from aiogram import Router, F, Bot
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION

from db.queries import get_intern
from db.queries.aisystant import get_aisystant_id
from db.queries.workshop import (
    get_workshop_payment_count,
    create_and_confirm_payment,
    log_community_join,
    log_community_leave,
    get_community_stats,
)
from i18n import t

logger = logging.getLogger(__name__)

workshop_router = Router(name="workshop")

# ── Config ─────────────────────────────────────────────

COMMUNITY_IWE_CHAT_ID = int(os.getenv("COMMUNITY_IWE_CHAT_ID", "-1003674048529"))
MASTERSKAYA_CHAT_ID = int(os.getenv("MASTERSKAYA_CHAT_ID", "-1002707216106"))
SEMINAR_VIDEO_URL = os.getenv("SEMINAR_VIDEO_URL", "https://t.me/c/3674048529/223")
SEMINAR_AMOUNT = 5000

MANAGED_CHAT_IDS = frozenset()


def _init_managed_chats():
    """Инициализировать set управляемых чатов (после загрузки env)."""
    global MANAGED_CHAT_IDS
    ids = set()
    if COMMUNITY_IWE_CHAT_ID:
        ids.add(COMMUNITY_IWE_CHAT_ID)
    if MASTERSKAYA_CHAT_ID:
        ids.add(MASTERSKAYA_CHAT_ID)
    MANAGED_CHAT_IDS = frozenset(ids)


# Вызывается при import — env уже загружен
_init_managed_chats()


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


# ── Меню «Семинар IWE» (заменяет старый sched_workshop) ──


@workshop_router.callback_query(F.data == "sched_workshop")
async def callback_seminar_iwe(callback: CallbackQuery):
    """Семинар IWE — показ по count оплат."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    count = await get_workshop_payment_count(chat_id)

    if count == 0:
        text = t('workshop.seminar_count0', lang)
        buttons = [
            [InlineKeyboardButton(
                text=t('workshop.btn_pay', lang),
                callback_data="seminar_iwe_pay",
            )],
        ]
    elif count == 1:
        text = t('workshop.seminar_count1', lang)
        buttons = [
            [InlineKeyboardButton(
                text=t('workshop.btn_pay', lang),
                callback_data="seminar_iwe_pay",
            )],
        ]
    else:
        text = t('workshop.seminar_count2', lang)
        buttons = []

    buttons.append([InlineKeyboardButton(
        text=t('schedule.btn_back', lang), callback_data="sched_back",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Оплата ─────────────────────────────────────────────


@workshop_router.callback_query(F.data == "seminar_iwe_pay")
async def callback_seminar_pay(callback: CallbackQuery):
    """Оплата семинара IWE (5000₽).

    Если есть aisystant_id → через Aisystant API (purpose=WORKSHOP).
    Если нет → прямая оплата через Telegram Stars / запись в workshop_payments.
    """
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    await callback.answer()

    aisystant_id = await get_aisystant_id(chat_id)

    if aisystant_id:
        # Через Aisystant API — Aisystant пришлёт webhook при успехе
        try:
            from clients.aisystant import aisystant
            result = await aisystant.create_subscription_payment(
                aisystant_id, "WORKSHOP_SEMINAR", SEMINAR_AMOUNT, purpose="WORKSHOP",
            )
            if result and result.get("confirmationUrl"):
                url = result["confirmationUrl"]
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('workshop.btn_pay_link', lang), url=url)],
                    [InlineKeyboardButton(text=t('workshop.btn_paid_check', lang),
                                          callback_data="seminar_iwe_check")],
                ])
                await callback.message.answer(
                    t('workshop.pay_redirect', lang), reply_markup=keyboard,
                )
                return
        except Exception as e:
            logger.error(f"[Workshop] Aisystant payment error for {chat_id}: {e}")
            # Fallback: покажем ошибку, но не блокируем
            await callback.message.answer(t('workshop.pay_error', lang))
            return
    else:
        # Без aisystant_id — прямая запись (оплата будет через Telegram Stars позже)
        # Пока: показываем сообщение о необходимости привязки или оплаты через витрину
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('workshop.btn_link_account', lang),
                callback_data="link_check",
            )],
            [InlineKeyboardButton(
                text=t('schedule.btn_back', lang), callback_data="sched_back",
            )],
        ])
        await callback.message.answer(
            t('workshop.pay_need_account', lang), reply_markup=keyboard,
        )


@workshop_router.callback_query(F.data == "seminar_iwe_check")
async def callback_seminar_check(callback: CallbackQuery):
    """Проверка оплаты — пересчитать count и показать результат."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    count = await get_workshop_payment_count(chat_id)

    if count == 0:
        await callback.answer(t('workshop.check_not_yet', lang), show_alert=True)
        return

    await callback.answer()
    # Оплата найдена → отправить invite
    await _send_invite_by_count(callback.message.bot, chat_id, count, lang, callback.message)


# ── Invite + Approve ───────────────────────────────────


async def _send_invite_by_count(bot: Bot, chat_id: int, count: int, lang: str, message=None):
    """Отправить invite-ссылку на основании count оплат."""
    if count >= 3:
        text = t('workshop.post_payment_count3', lang)
        if message:
            await message.answer(text)
        else:
            await bot.send_message(chat_id, text)
        return

    if count == 1:
        target_chat_id = COMMUNITY_IWE_CHAT_ID
    elif count == 2:
        target_chat_id = MASTERSKAYA_CHAT_ID
    else:
        return

    if not target_chat_id:
        logger.error(f"[Workshop] target chat not configured for count={count}")
        return

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=target_chat_id,
            member_limit=1,
            creates_join_request=True,
        )
        invite_url = invite.invite_link
    except Exception as e:
        logger.error(f"[Workshop] create_chat_invite_link error: {e}")
        text = t('workshop.invite_error', lang)
        if message:
            await message.answer(text)
        else:
            await bot.send_message(chat_id, text)
        return

    if count == 1:
        text = t('workshop.post_payment_count1', lang,
                  video_url=SEMINAR_VIDEO_URL, invite_url=invite_url)
    else:
        text = t('workshop.post_payment_count2', lang, invite_url=invite_url)

    if message:
        await message.answer(text, disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id, text, disable_web_page_preview=True)


@workshop_router.chat_join_request()
async def handle_community_join_request(request: ChatJoinRequest):
    """Одобрение/отклонение заявки на вход в Сообщество/Мастерскую."""
    request_chat_id = request.chat.id
    user_id = request.from_user.id

    if request_chat_id not in MANAGED_CHAT_IDS:
        return  # не наш чат

    count = await get_workshop_payment_count(user_id)

    if request_chat_id == COMMUNITY_IWE_CHAT_ID and count >= 1:
        await request.approve()
        logger.info(f"[Workshop] approved join: tg={user_id}, chat=community, count={count}")
    elif request_chat_id == MASTERSKAYA_CHAT_ID and count >= 2:
        await request.approve()
        logger.info(f"[Workshop] approved join: tg={user_id}, chat=masterskaya, count={count}")
    else:
        await request.decline()
        logger.info(f"[Workshop] declined join: tg={user_id}, chat={request_chat_id}, count={count}")


# ── ChatMember logging ─────────────────────────────────


@workshop_router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_community_member_join(event: ChatMemberUpdated):
    """Логирование вступления в управляемый чат."""
    if event.chat.id not in MANAGED_CHAT_IDS:
        return

    user = event.new_chat_member.user
    # Определяем source: была ли оплата перед вступлением?
    count = await get_workshop_payment_count(user.id)
    source = "payment" if count > 0 else "admin"

    await log_community_join(
        telegram_id=user.id,
        chat_id=event.chat.id,
        username=user.username,
        first_name=user.first_name,
        source=source,
    )


@workshop_router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_community_member_leave(event: ChatMemberUpdated):
    """Логирование выхода из управляемого чата."""
    if event.chat.id not in MANAGED_CHAT_IDS:
        return

    user = event.old_chat_member.user
    await log_community_leave(telegram_id=user.id, chat_id=event.chat.id)


# ── /community_report (admin) ──────────────────────────

DEVELOPER_CHAT_ID = int(os.getenv("DEVELOPER_CHAT_ID", "0"))


@workshop_router.message(Command("community_report"))
async def cmd_community_report(message: Message):
    """Admin-отчёт по чатам Сообщества и Мастерской."""
    if message.chat.id != DEVELOPER_CHAT_ID and message.from_user.id != DEVELOPER_CHAT_ID:
        return

    # Парсим период: /community_report week (default) или /community_report day
    args = message.text.strip().split()
    period = args[1] if len(args) > 1 else "week"
    days = 1 if period == "day" else 7
    period_label = "сутки" if days == 1 else "неделю"

    parts = []

    for chat_id, chat_name in [
        (COMMUNITY_IWE_CHAT_ID, "Сообщество IWE"),
        (MASTERSKAYA_CHAT_ID, "Мастерская Церена"),
    ]:
        if not chat_id:
            continue

        stats = await get_community_stats(chat_id, days=days)
        new_count = len(stats["new"])
        left_count = len(stats["left"])

        lines = [
            f"📊 <b>{chat_name}</b> — отчёт за {period_label}",
            "",
            f"Всего участников: {stats['total']} (+{new_count})",
            f"Оплативших: {stats['paid']}",
            f"Бесплатных: {stats['free']}",
        ]

        if stats["new"]:
            lines.append("")
            lines.append("Новые за период:")
            for m in stats["new"][:10]:
                name = f"@{m['username']}" if m.get("username") else (m.get("first_name") or "—")
                if m.get("amount"):
                    lines.append(f"  • {name} — оплата {int(m['amount'])}₽")
                else:
                    lines.append(f"  • {name} — добавлен админом")

        lines.append(f"\nВышли: {left_count}")
        parts.append("\n".join(lines))

    if not parts:
        await message.answer("Чаты не настроены (COMMUNITY_IWE_CHAT_ID / MASTERSKAYA_CHAT_ID).")
        return

    await message.answer("\n\n".join(parts), parse_mode="HTML")


# ── Обработка webhook от Aisystant ─────────────────────


async def process_workshop_webhook(data: dict, bot: Bot) -> dict:
    """Обработать webhook от Aisystant при оплате WORKSHOP.

    Вызывается из oauth_server.py → POST /webhook/workshop-payment.
    """
    telegram_id = data.get("telegram_id")
    amount = data.get("amount", SEMINAR_AMOUNT)
    payment_id = data.get("payment_id")

    if not telegram_id:
        return {"ok": False, "error": "missing telegram_id"}

    telegram_id = int(telegram_id)

    # Получаем aisystant_id если есть связка
    aisystant_id = await get_aisystant_id(telegram_id)

    # Создаём подтверждённую оплату
    row_id = await create_and_confirm_payment(
        telegram_id=telegram_id,
        amount=amount,
        source="aisystant_webhook",
        aisystant_id=aisystant_id,
        payment_id=payment_id,
    )

    # Отправляем invite
    count = await get_workshop_payment_count(telegram_id)
    intern = await get_intern(telegram_id)
    lang = _lang(intern)

    await _send_invite_by_count(bot, telegram_id, count, lang)

    logger.info(f"[Workshop] webhook processed: tg={telegram_id}, count={count}, row={row_id}")
    return {"ok": True, "count": count, "payment_row_id": row_id}
