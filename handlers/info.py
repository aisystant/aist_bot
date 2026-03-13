"""
Info-команды: навигационные landing pages.

/about — О нас (экосистема, бот, платформа Aisystant, с чего начать)
/marathon_info — Марафон (что, зачем, как начать, настройка)
/feed_info — Лента (что, зачем, для кого, как запустить)
/train_info — Тренировка (что, зачем, для кого, как запустить)

На T1: info + путь к покупке/привязке.
На T2+: info + прямой запуск.
"""

import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from core.tier_config import UITier
from core.tier_detector import detect_ui_tier
from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

info_router = Router(name="info")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


# ═══════════════════════════════════════════════════════════
# /about — О нас
# ═══════════════════════════════════════════════════════════

@info_router.message(Command("about"))
async def cmd_about(message: Message):
    """О нас: экосистема, бот, платформа, с чего начать."""
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    tier = await detect_ui_tier(message.chat.id)

    buttons = []
    if tier == UITier.T1_NEW:
        buttons.append([
            InlineKeyboardButton(text=t('info.btn_marathon', lang), callback_data="info_marathon"),
            InlineKeyboardButton(text=t('info.btn_link', lang), callback_data="info_go_link"),
        ])
    elif tier == UITier.T1_START:
        buttons.append([
            InlineKeyboardButton(text=t('info.btn_marathon', lang), callback_data="info_marathon"),
            InlineKeyboardButton(text=t('info.btn_buy', lang), callback_data="info_go_buy"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text=t('info.btn_marathon', lang), callback_data="info_marathon"),
            InlineKeyboardButton(text=t('info.btn_feed', lang), callback_data="info_feed"),
        ])
        buttons.append([
            InlineKeyboardButton(text=t('info.btn_training', lang), callback_data="info_training"),
            InlineKeyboardButton(text=t('info.btn_ask', lang), callback_data="info_ask_hint"),
        ])

    if tier in (UITier.T1_NEW, UITier.T1_START):
        buttons.append([InlineKeyboardButton(
            text=t('info.btn_ask', lang),
            callback_data="info_ask_hint",
        )])

    await message.answer(
        t('info.about_text', lang),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ═══════════════════════════════════════════════════════════
# /marathon_info — Марафон
# ═══════════════════════════════════════════════════════════

@info_router.message(Command("marathon_info"))
async def cmd_marathon_info(message: Message):
    """Марафон: что, зачем, как начать."""
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    await _show_marathon_info(message, lang, edit=False)


@info_router.callback_query(F.data == "info_marathon")
async def cb_marathon_info(callback: CallbackQuery):
    """Inline-переход к Marathon info."""
    intern = await get_intern(callback.message.chat.id)
    lang = _lang(intern)
    await callback.answer()
    await _show_marathon_info(callback.message, lang, edit=True)


async def _show_marathon_info(message: Message, lang: str, edit: bool = False):
    """Показать info о Марафоне с кнопками запуска/настройки."""
    chat_id = message.chat.id
    tier = await detect_ui_tier(chat_id)

    buttons = []
    if tier in (UITier.T1_NEW, UITier.T1_START):
        buttons.append([
            InlineKeyboardButton(text=t('info.btn_start_marathon', lang), callback_data="info_go_learn"),
            InlineKeyboardButton(text=t('info.btn_configure_marathon', lang), callback_data="info_go_profile"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text=t('info.btn_get_lesson', lang), callback_data="info_go_learn"),
            InlineKeyboardButton(text=t('info.btn_configure_marathon', lang), callback_data="info_go_profile"),
        ])

    text = t('info.marathon_text', lang)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)


# ═══════════════════════════════════════════════════════════
# /feed_info — Лента
# ═══════════════════════════════════════════════════════════

@info_router.message(Command("feed_info"))
async def cmd_feed_info(message: Message):
    """Лента: что, зачем, для кого, как запустить."""
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    await _show_feed_info(message, lang, edit=False)


@info_router.callback_query(F.data == "info_feed")
async def cb_feed_info(callback: CallbackQuery):
    """Inline-переход к Feed info."""
    intern = await get_intern(callback.message.chat.id)
    lang = _lang(intern)
    await callback.answer()
    await _show_feed_info(callback.message, lang, edit=True)


async def _show_feed_info(message: Message, lang: str, edit: bool = False):
    """Показать info о Ленте с кнопками запуска/покупки."""
    chat_id = message.chat.id
    tier = await detect_ui_tier(chat_id)

    buttons = []
    if tier >= UITier.T2_LEARNING:
        buttons.append([
            InlineKeyboardButton(text=t('info.btn_get_digest', lang), callback_data="info_go_feed"),
            InlineKeyboardButton(text=t('info.btn_configure_feed', lang), callback_data="info_go_profile"),
        ])
    else:
        buttons.append([InlineKeyboardButton(
            text=t('info.btn_buy_sub', lang),
            callback_data="info_go_buy",
        )])

    text = t('info.feed_text', lang)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)


# ═══════════════════════════════════════════════════════════
# /train_info — Тренировка
# ═══════════════════════════════════════════════════════════

@info_router.message(Command("train_info"))
async def cmd_train_info(message: Message):
    """Тренировка: что, зачем, для кого, как запустить."""
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    await _show_train_info(message, lang, edit=False)


@info_router.callback_query(F.data == "info_training")
async def cb_train_info(callback: CallbackQuery):
    """Inline-переход к Training info."""
    intern = await get_intern(callback.message.chat.id)
    lang = _lang(intern)
    await callback.answer()
    await _show_train_info(callback.message, lang, edit=True)


async def _show_train_info(message: Message, lang: str, edit: bool = False):
    """Показать info о Тренировке с кнопками запуска/покупки."""
    chat_id = message.chat.id
    tier = await detect_ui_tier(chat_id)

    buttons = []
    if tier >= UITier.T2_LEARNING:
        buttons.append([
            InlineKeyboardButton(text=t('info.btn_start_training', lang), callback_data="info_go_train"),
            InlineKeyboardButton(text=t('info.btn_configure_training', lang), callback_data="info_go_profile"),
        ])
    else:
        buttons.append([InlineKeyboardButton(
            text=t('info.btn_buy_sub', lang),
            callback_data="info_go_buy",
        )])

    text = t('info.train_text', lang)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)


# ═══════════════════════════════════════════════════════════
# Callback routing — навигация к существующим командам
# ═══════════════════════════════════════════════════════════

@info_router.callback_query(F.data == "info_go_link")
async def cb_go_link(callback: CallbackQuery):
    """Переход к /link."""
    await callback.answer()
    from handlers.link import cmd_link
    await cmd_link(callback.message)


@info_router.callback_query(F.data == "info_go_buy")
async def cb_go_buy(callback: CallbackQuery):
    """Переход к /buy."""
    await callback.answer()
    from handlers.buy import cmd_buy
    await cmd_buy(callback.message)


@info_router.callback_query(F.data == "info_go_learn")
async def cb_go_learn(callback: CallbackQuery):
    """Переход к /learn (марафон) через dispatcher."""
    await callback.answer()
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        await dispatcher.route_learn(intern)
    else:
        lang = _lang(intern)
        await callback.message.answer(t('info.use_learn', lang))


@info_router.callback_query(F.data == "info_go_feed")
async def cb_go_feed(callback: CallbackQuery):
    """Переход к /feed через dispatcher."""
    await callback.answer()
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        await dispatcher.route_command('feed', intern)
    else:
        lang = _lang(intern)
        await callback.message.answer(t('info.use_feed', lang))


@info_router.callback_query(F.data == "info_go_train")
async def cb_go_train(callback: CallbackQuery):
    """Переход к /train через dispatcher."""
    await callback.answer()
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        await dispatcher.route_command('train', intern)
    else:
        lang = _lang(intern)
        await callback.message.answer(t('info.use_train', lang))


@info_router.callback_query(F.data == "info_go_profile")
async def cb_go_profile(callback: CallbackQuery):
    """Переход к /profile через dispatcher."""
    await callback.answer()
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        await dispatcher.route_command('profile', intern)
    else:
        lang = _lang(intern)
        await callback.message.answer(t('info.use_settings', lang))


@info_router.callback_query(F.data == "info_ask_hint")
async def cb_ask_hint(callback: CallbackQuery):
    """Подсказка про '?' вопрос."""
    intern = await get_intern(callback.message.chat.id)
    lang = _lang(intern)
    await callback.answer()
    await callback.message.edit_text(
        t('info.ask_hint_text', lang),
        parse_mode="Markdown",
    )
