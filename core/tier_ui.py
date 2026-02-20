"""
Tier-aware UI builders: ReplyKeyboard, Menu Commands sync.

Source-of-truth: WP-52 § 3-4

Usage:
    from core.tier_ui import send_tier_keyboard, sync_menu_commands

    # On /start or tier transition:
    await send_tier_keyboard(message, user)

    # Or individually:
    keyboard = build_reply_keyboard(tier, lang)
    await sync_menu_commands(bot, user_id, tier, lang)
"""

import logging

from aiogram import Bot
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup, KeyboardButton,
    BotCommand, BotCommandScopeChat,
)

from core.tier_config import (
    UITier, TIER_KEYBOARD, TIER_MENU_COMMANDS,
    KB_LABELS, COMMAND_DESCRIPTIONS,
)
from core.tier_detector import detect_ui_tier

logger = logging.getLogger(__name__)


def build_reply_keyboard(tier: int, lang: str = 'ru') -> ReplyKeyboardMarkup:
    """Build 2x2 ReplyKeyboard for a given tier.

    Args:
        tier: UITier constant (1-5)
        lang: language code

    Returns:
        ReplyKeyboardMarkup with 2x2 grid, persistent
    """
    layout = TIER_KEYBOARD.get(tier, TIER_KEYBOARD[UITier.T1_START])

    keyboard = []
    for row in layout:
        kb_row = []
        for service_key in row:
            labels = KB_LABELS.get(service_key, {})
            text = labels.get(lang, labels.get('ru', service_key))
            kb_row.append(KeyboardButton(text=text))
        keyboard.append(kb_row)

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
    )


async def sync_menu_commands(bot: Bot, user_id: int, tier: int, lang: str = 'ru') -> None:
    """Set per-user menu commands (Bot Menu Button) based on tier.

    Uses BotCommandScopeChat for per-user command menus.
    """
    command_keys = TIER_MENU_COMMANDS.get(tier, TIER_MENU_COMMANDS[UITier.T1_START])

    commands = []
    for cmd_key in command_keys:
        descriptions = COMMAND_DESCRIPTIONS.get(cmd_key, {})
        desc = descriptions.get(lang, descriptions.get('ru', cmd_key))
        commands.append(BotCommand(command=cmd_key, description=desc))

    try:
        await bot.set_my_commands(
            commands,
            scope=BotCommandScopeChat(chat_id=user_id),
        )
    except Exception as e:
        logger.warning(f"[TierUI] Failed to sync menu for user={user_id}: {e}")


async def send_tier_keyboard(message: Message, user: dict, text: str = None) -> None:
    """Detect tier and send ReplyKeyboard + sync menu commands.

    Args:
        message: aiogram Message (to reply to)
        user: intern dict from get_intern()
        text: optional message text; if None, keyboard is attached silently
    """
    tier = detect_ui_tier(user)
    lang = user.get('language', 'ru') or 'ru'
    keyboard = build_reply_keyboard(tier, lang)

    if text:
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        # Telegram requires a message to attach keyboard
        await message.answer("⌨️", reply_markup=keyboard)

    # Sync hamburger menu commands for this user
    await sync_menu_commands(message.bot, user['chat_id'], tier, lang)
