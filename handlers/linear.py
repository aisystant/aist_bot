"""
Хендлеры интеграции с Linear (OAuth, задачи).
"""

import logging

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

linear_router = Router(name="linear")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@linear_router.message(Command("linear"))
async def cmd_linear(message: Message):
    """Команда для интеграции с Linear."""
    from clients.linear_oauth import linear_oauth

    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    telegram_user_id = message.chat.id

    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    subcommand = parts[1].lower() if len(parts) > 1 else None

    is_connected = linear_oauth.is_connected(telegram_user_id)

    if subcommand == "disconnect":
        if is_connected:
            linear_oauth.disconnect(telegram_user_id)
            await message.answer(t('linear.disconnected', lang))
        else:
            await message.answer(t('linear.not_connected', lang))
        return

    if subcommand == "tasks":
        if not is_connected:
            await message.answer(t('linear.not_connected_desc', lang))
            return

        await message.answer(t('linear.loading_tasks', lang))
        issues = await linear_oauth.get_my_issues(telegram_user_id, limit=10)

        if issues is None:
            await message.answer(t('linear.tasks_error', lang))
            return

        if not issues:
            await message.answer(t('linear.no_tasks', lang))
            return

        lines = [f"{t('linear.tasks_title', lang)}\n"]
        for issue in issues:
            state_name = issue.get("state", {}).get("name", "?")
            identifier = issue.get("identifier", "?")
            title = issue.get("title", t('linear.no_title', lang))
            url = issue.get("url", "")
            lines.append(f"• [{identifier}]({url}) — {title}\n  _{state_name}_")

        await message.answer("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)
        return

    if is_connected:
        viewer = await linear_oauth.get_viewer(telegram_user_id)
        name = viewer.get("name", t('linear.user_fallback', lang)) if viewer else t('linear.user_fallback', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('linear.btn_my_tasks', lang), callback_data="linear_tasks")],
            [InlineKeyboardButton(text=t('linear.btn_disconnect', lang), callback_data="linear_disconnect")]
        ])

        await message.answer(
            f"*{t('linear.connected_title', lang)}*\n\n"
            f"{t('linear.authorized_as', lang, name=name)}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        try:
            auth_url, state = linear_oauth.get_authorization_url(telegram_user_id)

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('linear.btn_connect', lang), url=auth_url)]
            ])

            await message.answer(
                f"*{t('linear.connect_title', lang)}*\n\n"
                f"{t('linear.connect_desc', lang)}",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except ValueError as e:
            await message.answer(t('linear.config_error', lang, error=str(e)))


@linear_router.callback_query(F.data == "linear_tasks")
async def callback_linear_tasks(callback: CallbackQuery):
    try:
        from clients.linear_oauth import linear_oauth
    except ImportError:
        await callback.answer(t('linear.not_configured', 'ru'), show_alert=True)
        return

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not linear_oauth.is_connected(telegram_user_id):
        await callback.answer(t('linear.not_connected_alert', lang), show_alert=True)
        return

    await callback.answer()

    issues = await linear_oauth.get_my_issues(telegram_user_id, limit=10)

    if not issues:
        await callback.message.answer(t('linear.no_tasks_short', lang))
        return

    lines = [f"{t('linear.tasks_title_short', lang)}\n"]
    for issue in issues:
        state_name = issue.get("state", {}).get("name", "?")
        identifier = issue.get("identifier", "?")
        title = issue.get("title", t('linear.no_title', lang))
        url = issue.get("url", "")
        lines.append(f"• [{identifier}]({url}) — {title}\n  _{state_name}_")

    await callback.message.answer("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


@linear_router.callback_query(F.data == "linear_disconnect")
async def callback_linear_disconnect(callback: CallbackQuery):
    try:
        from clients.linear_oauth import linear_oauth
    except ImportError:
        await callback.answer(t('linear.not_configured', 'ru'), show_alert=True)
        return

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not linear_oauth.is_connected(telegram_user_id):
        await callback.answer(t('linear.already_disconnected', lang), show_alert=True)
        return

    linear_oauth.disconnect(telegram_user_id)
    await callback.answer(t('linear.disconnected_alert', lang), show_alert=True)

    await callback.message.edit_text(
        f"*{t('linear.disconnected_title', lang)}*\n\n"
        f"{t('linear.reconnect_hint', lang)}",
        parse_mode="Markdown"
    )
