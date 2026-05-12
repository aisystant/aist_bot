"""
Хэндлер привязки Telegram ↔ Aisystant аккаунта (WP-79).

Команды:
- /link — привязать аккаунт (или кнопка «🔗 Привязать»)
- callback link_check — повторная проверка после перехода на сайт

Поток:
1. Проверяем, есть ли уже привязка в БД
2. Если нет → спрашиваем Aisystant API (find_user_by_tg)
3. Если найден → сохраняем aisystant_id в БД
4. Если нет → показываем ссылку для привязки на сайте + кнопку «Проверить»
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
from db.queries.aisystant import get_aisystant_id, save_aisystant_link
from clients.aisystant import aisystant
from i18n import t

logger = logging.getLogger(__name__)

link_router = Router(name="link")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@link_router.message(Command("link"))
async def cmd_link(message: Message):
    """Команда /link — привязка аккаунта Aisystant."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    # Уже привязан?
    existing = await get_aisystant_id(chat_id)
    if existing:
        await message.answer(t('link.already_linked', lang))
        await _refresh_tier_keyboard(message, chat_id, lang)
        return

    # Пробуем найти автоматически
    try:
        aisystant_id = await aisystant.find_user_by_tg(chat_id)
    except Exception as e:
        logger.error(f"[Link] find_user_by_tg error for {chat_id}: {e}")
        await message.answer(t('link.error', lang))
        return

    if aisystant_id:
        await save_aisystant_link(chat_id, aisystant_id)
        # Склейка оплат семинара (WP-181)
        await _migrate_workshop_payments(chat_id, aisystant_id)
        await message.answer(t('link.found_auto', lang))
        # Обновляем тир и клавиатуру
        logger.info(f"[Link] ABOUT TO CALL _refresh_tier_keyboard for {chat_id}")
        await _refresh_tier_keyboard(message, chat_id, lang)
        # Показываем что делать дальше
        await _send_link_next_steps(message, chat_id, lang)
        # WP-188 Ф17 follow-up: предложить consent для тех, кто пришёл к /link уже
        # после онбординга (auto-link не сработал на /start) — иначе они не увидят
        # inline-кнопки consent и не узнают про opt-in.
        await message.answer(
            "📊 <b>Хочешь, чтобы платформа считала твою ступень мастерства?</b>\n\n"
            "Для этого нужно одно действие — согласие на трекинг развития.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📊 Согласие на трекинг", callback_data="consent_from_onboarding"),
            ]]),
        )
        return

    # Не найден → показываем ссылку для привязки
    try:
        tg_username = message.from_user.username if message.from_user else None
        link_url = await aisystant.get_link_url(chat_id, tg_username)
    except Exception as e:
        logger.error(f"[Link] get_link_url error for {chat_id}: {e}")
        await message.answer(t('link.error', lang))
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('link.btn_link', lang), url=link_url)],
        [InlineKeyboardButton(text=t('link.btn_check', lang), callback_data="link_check")],
    ])

    await message.answer(t('link.not_found', lang), reply_markup=keyboard)


@link_router.callback_query(F.data == "link_check")
async def callback_link_check(callback: CallbackQuery):
    """Повторная проверка привязки после перехода на сайт."""
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    # Уже привязан?
    existing = await get_aisystant_id(chat_id)
    if existing:
        await callback.answer(t('link.check_success', lang), show_alert=True)
        await callback.message.edit_text(t('link.check_success', lang))
        return

    # Проверяем ещё раз через API
    try:
        aisystant_id = await aisystant.find_user_by_tg(chat_id)
    except Exception as e:
        logger.error(f"[Link] check: find_user_by_tg error for {chat_id}: {e}")
        await callback.answer(t('link.error', lang), show_alert=True)
        return

    if aisystant_id:
        await save_aisystant_link(chat_id, aisystant_id)
        # Склейка оплат семинара (WP-181)
        await _migrate_workshop_payments(chat_id, aisystant_id)
        await callback.answer(t('link.check_success', lang), show_alert=True)
        await callback.message.edit_text(t('link.check_success', lang))
        # Обновляем тир и клавиатуру
        logger.info(f"[Link] callback: ABOUT TO CALL _refresh_tier_keyboard for {chat_id}")
        await _refresh_tier_keyboard(callback.message, chat_id, lang)
        logger.info(f"[Link] callback: _refresh_tier_keyboard returned for {chat_id}")
        # Показываем что делать дальше
        logger.info(f"[Link] callback: ABOUT TO CALL _send_link_next_steps for {chat_id}")
        await _send_link_next_steps(callback.message, chat_id, lang)
        logger.info(f"[Link] callback: _send_link_next_steps returned for {chat_id}")
    else:
        await callback.answer(t('link.check_not_yet', lang), show_alert=True)


async def _migrate_workshop_payments(chat_id: int, aisystant_id: str):
    """При склейке — привязать оплаты семинара к aisystant_id (WP-181)."""
    try:
        from db.queries.workshop import migrate_payments_to_aisystant
        await migrate_payments_to_aisystant(chat_id, aisystant_id)
    except Exception as e:
        logger.error(f"[Link] workshop payment migration error: {e}")


async def _refresh_tier_keyboard(message, chat_id: int, lang: str):
    """Обновить ReplyKeyboard и меню после смены тира."""
    logger.info(f"[Link] _refresh_tier_keyboard ENTERED for {chat_id}")
    try:
        logger.info(f"[Link] importing tier modules...")
        from core.tier_detector import detect_ui_tier
        from core.tier_ui import build_reply_keyboard, sync_menu_commands
        from db.queries.aisystant import get_aisystant_id
        logger.info(f"[Link] imports successful")

        # Диагностика: проверяем что aisystant_id действительно записан
        logger.info(f"[Link] calling get_aisystant_id...")
        aisystant_id = await get_aisystant_id(chat_id)
        logger.info(f"[Link] tier_keyboard debug: chat_id={chat_id}, aisystant_id={aisystant_id}")

        tier = await detect_ui_tier(chat_id)
        logger.info(f"[Link] tier detected: chat_id={chat_id}, tier={tier}")

        keyboard = build_reply_keyboard(tier, lang)
        logger.debug(f"[Link] keyboard built for tier {tier}, sending to {chat_id}")

        logger.info(f"[Link] SENDING keyboard message (👌) to {chat_id}...")
        await message.answer("👌", reply_markup=keyboard)
        logger.info(f"[Link] keyboard message sent to {chat_id}")

        logger.info(f"[Link] syncing menu commands for {chat_id}...")
        await sync_menu_commands(message.bot, chat_id, tier, lang)
        logger.info(f"[Link] menu commands synced for {chat_id}, tier={tier}")
        logger.info(f"[Link] _refresh_tier_keyboard COMPLETED SUCCESSFULLY for {chat_id}")
    except Exception as e:
        logger.error(f"[Link] refresh tier keyboard error for {chat_id}: {e}", exc_info=True)
        logger.error(f"[Link] FAILED to refresh tier keyboard for {chat_id}")


async def _send_link_next_steps(message, chat_id: int, lang: str):
    """Показать 'что делать дальше' после успешной привязки."""
    try:
        from core.tier_detector import detect_ui_tier
        from core.tier_config import UITier
        from config import AISYSTANT_BASE_URL

        tier = await detect_ui_tier(chat_id)

        if tier <= UITier.T1:
            text = t('link.next_steps_no_sub', lang)
            buttons = [
                [InlineKeyboardButton(text=t('link.btn_subscribe', lang), callback_data="aisystant_subscribe")],
                [InlineKeyboardButton(text=t('link.btn_connect_ai', lang), callback_data="iwe_connect_start")],
                [InlineKeyboardButton(text=t('link.btn_programs', lang), url=f"{AISYSTANT_BASE_URL}/programs")],
            ]
        else:
            text = t('link.next_steps_with_sub', lang)
            buttons = [
                [InlineKeyboardButton(text=t('link.btn_connect_ai', lang), callback_data="iwe_connect_start")],
                [InlineKeyboardButton(text=t('link.btn_programs', lang), url=f"{AISYSTANT_BASE_URL}/programs")],
            ]

        await message.answer(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"[Link] next steps error: {e}")
