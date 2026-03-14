"""
/features — Platform Feature Catalog.

Shows all platform features with per-user status:
  ✅ Connected / available
  🔒 Requires higher tier
  ❌ Not connected (action needed)
  💻 External (set up on your computer)

Groups by category: Learning, Productivity, Integration, Automation.
"""

import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from core.tier_config import UITier
from core.tier_detector import detect_ui_tier
from core.platform_features import PLATFORM_FEATURES, get_features_by_category, PlatformFeature
from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

features_router = Router(name="features")

# Category display order and i18n keys
CATEGORY_ORDER = ["learning", "productivity", "integration", "automation"]
CATEGORY_ICONS = {
    "learning": "📚",
    "productivity": "⚡",
    "integration": "🔗",
    "automation": "🤖",
}


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


async def _get_feature_status(feature: PlatformFeature, chat_id: int, tier: int) -> str:
    """Determine status icon for a feature."""
    if feature.min_tier > tier:
        return "🔒"

    if feature.status_check:
        try:
            result = await feature.status_check(chat_id)
            if result is None:
                return "💻"  # external, set up on computer
            return "✅" if result else "❌"
        except Exception:
            return "❓"

    return "✅"


async def _build_features_text(chat_id: int, lang: str) -> str:
    """Build the full features catalog text."""
    tier = await detect_ui_tier(chat_id)
    categories = get_features_by_category()

    lines = [t('features.header', lang)]
    lines.append(t('features.tier_info', lang, tier=tier))
    lines.append("")

    for cat_id in CATEGORY_ORDER:
        features = categories.get(cat_id, [])
        if not features:
            continue

        cat_icon = CATEGORY_ICONS.get(cat_id, "")
        cat_name = t(f'features.cat_{cat_id}', lang)
        lines.append(f"{cat_icon} *{cat_name}*")

        for feature in features:
            status = await _get_feature_status(feature, chat_id, tier)
            name = t(f'features.{feature.i18n_key}.name', lang)
            desc = t(f'features.{feature.i18n_key}.desc', lang)

            if feature.command:
                lines.append(f"  {status} {feature.icon} {name} — {desc} ({feature.command})")
            else:
                lines.append(f"  {status} {feature.icon} {name} — {desc}")

        lines.append("")

    # Legend
    lines.append(t('features.legend', lang))

    return "\n".join(lines)


@features_router.message(Command("features"))
async def cmd_features(message: Message):
    """Show platform feature catalog with per-user status."""
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    tier = await detect_ui_tier(message.chat.id)

    text = await _build_features_text(message.chat.id, lang)

    # Build action buttons based on tier
    buttons = []

    if tier < UITier.T2_LEARNING:
        buttons.append([InlineKeyboardButton(
            text=t('features.btn_upgrade', lang),
            callback_data="features_go_buy",
        )])
    if tier < UITier.T4_CREATION:
        buttons.append([InlineKeyboardButton(
            text=t('features.btn_next_tier', lang),
            callback_data="features_next_tier",
        )])

    buttons.append([InlineKeyboardButton(
        text=t('features.btn_about', lang),
        callback_data="features_go_about",
    )])

    await message.answer(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )


# ═══════════════════════════════════════════════════════════
# Detail view — per-feature setup instructions
# ═══════════════════════════════════════════════════════════

@features_router.callback_query(F.data == "features_next_tier")
async def cb_next_tier(callback: CallbackQuery):
    """Show what's needed for the next tier — as a separate message (catalog stays)."""
    await callback.answer()
    intern = await get_intern(callback.message.chat.id)
    lang = _lang(intern)
    tier = await detect_ui_tier(callback.message.chat.id)

    if tier == UITier.T0:
        text = t('features.next_t1', lang)
    elif tier == UITier.T1:
        text = t('features.next_t2', lang)
    elif tier == UITier.T2_LEARNING:
        text = t('features.next_t3', lang)
    elif tier == UITier.T3_PERSONALIZATION:
        text = t('features.next_t4', lang)
    else:
        text = t('features.max_tier', lang)

    # Send as a NEW message so the catalog remains visible
    await callback.message.answer(
        text,
        parse_mode="Markdown",
    )


@features_router.callback_query(F.data == "features_go_buy")
async def cb_go_buy(callback: CallbackQuery):
    """Navigate to /buy."""
    await callback.answer()
    from handlers.buy import cmd_buy
    await cmd_buy(callback.message)


@features_router.callback_query(F.data == "features_go_about")
async def cb_go_about(callback: CallbackQuery):
    """Navigate to /about."""
    await callback.answer()
    from handlers.info import cmd_about
    await cmd_about(callback.message)
