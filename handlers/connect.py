"""
IWE Connect wizard — подключение AI-клиентов (WP-209 Ф1).

Команда /connect показывает статус IWE-подключений и помогает
пользователю настроить claude.ai, Cursor, ChatGPT, GitHub.

Связь с WP-199: после реализации нового онбординга (Ф3)
кнопка «Подключить IWE» вызовет этот wizard.
"""

import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from config import GATEWAY_MCP_URL
from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

connect_router = Router(name="connect")


# ============= MAIN MENU =============

def _build_status_text(lang: str, gateway: bool, github: bool) -> str:
    """Текст статуса подключений."""
    def chk(ok):
        return "✅" if ok else "☐"

    return (
        f"*{t('connect.title', lang)}*\n\n"
        f"{t('connect.subtitle', lang)}\n\n"
        f"{chk(gateway)} {t('connect.status_gateway', lang)}\n"
        f"{chk(github)} GitHub\n\n"
        f"{t('connect.choose_client', lang)}"
    )


def _build_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора клиента."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Claude (claude.ai)", callback_data="iwe_claude")],
        [InlineKeyboardButton(text="⌨️ Cursor / Windsurf / Cline", callback_data="iwe_cursor")],
        [InlineKeyboardButton(text="💬 ChatGPT", callback_data="iwe_chatgpt")],
        [
            InlineKeyboardButton(text="🐙 GitHub", callback_data="iwe_github"),
            InlineKeyboardButton(text="🌐 Gateway", callback_data="iwe_gateway"),
        ],
        [InlineKeyboardButton(text=t('connect.done', lang), callback_data="iwe_close")],
    ])


@connect_router.message(Command("connect"))
async def cmd_connect(message: Message):
    """Команда /connect — IWE setup wizard."""
    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('connect.need_start', lang))
        return

    lang = intern.get('language', 'ru') or 'ru'
    chat_id = message.chat.id

    gateway, github = await _check_connections(chat_id)
    text = _build_status_text(lang, gateway, github)
    keyboard = _build_menu_keyboard(lang)

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@connect_router.callback_query(F.data == "iwe_connect_start")
async def on_connect_start(callback: CallbackQuery):
    """Точка входа из onboarding (inline-кнопка после /start)."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    if not intern:
        return
    lang = intern.get('language', 'ru') or 'ru'

    gateway, github = await _check_connections(chat_id)
    text = _build_status_text(lang, gateway, github)
    keyboard = _build_menu_keyboard(lang)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ============= CLIENT INSTRUCTIONS =============

def _back_button(lang: str) -> list:
    return [InlineKeyboardButton(text=t('connect.back_to_list', lang), callback_data="iwe_back")]


@connect_router.callback_query(F.data == "iwe_claude")
async def on_claude(callback: CallbackQuery):
    """Инструкция подключения claude.ai."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    text = t('connect.claude_instructions', lang, gateway_url=GATEWAY_MCP_URL)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=t('connect.open_claude', lang),
            url="https://claude.ai/settings/integrations",
        )],
        _back_button(lang),
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")


@connect_router.callback_query(F.data == "iwe_cursor")
async def on_cursor(callback: CallbackQuery):
    """Инструкция подключения Cursor / Windsurf / Cline."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    text = t('connect.cursor_instructions', lang, gateway_url=GATEWAY_MCP_URL)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        _back_button(lang),
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")


@connect_router.callback_query(F.data == "iwe_chatgpt")
async def on_chatgpt(callback: CallbackQuery):
    """Инструкция подключения ChatGPT."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    text = t('connect.chatgpt_instructions', lang, gateway_url=GATEWAY_MCP_URL)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        _back_button(lang),
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")


@connect_router.callback_query(F.data == "iwe_github")
async def on_github(callback: CallbackQuery):
    """GitHub — перенаправляем в Settings для OAuth."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    chat_id = callback.from_user.id

    try:
        from clients.github_oauth import github_oauth
        is_connected = await github_oauth.is_connected(chat_id)

        if is_connected:
            user_info = await github_oauth.get_user(chat_id)
            login = user_info.get("login", "?") if user_info else "?"
            text = t('connect.github_connected', lang, login=login)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                _back_button(lang),
            ])
        else:
            from config import GITHUB_CLIENT_ID
            if not GITHUB_CLIENT_ID:
                text = t('connect.gateway_unavailable', lang)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[_back_button(lang)])
            else:
                auth_url, _ = await github_oauth.get_authorization_url(chat_id)
                text = t('connect.github_instructions', lang)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🐙 " + t('connect.connect_github', lang), url=auth_url)],
                    _back_button(lang),
                ])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[Connect] GitHub error for {chat_id}: {e}")
        await callback.message.edit_text(
            t('connect.gateway_unavailable', lang),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_back_button(lang)]),
        )


@connect_router.callback_query(F.data == "iwe_gateway")
async def on_gateway(callback: CallbackQuery):
    """Gateway — Ory OAuth подключение."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    chat_id = callback.from_user.id

    try:
        from clients.gateway_mcp import gateway_mcp
        is_connected = gateway_mcp.is_connected(chat_id)

        if is_connected:
            text = t('connect.gateway_connected', lang)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                _back_button(lang),
            ])
        else:
            from clients.ory_oauth import ory_oauth
            from config import ORY_CLIENT_ID
            if not ORY_CLIENT_ID:
                text = t('connect.gateway_unavailable', lang)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[_back_button(lang)])
            else:
                auth_url, _ = await ory_oauth.get_authorization_url(chat_id)
                text = t('connect.gateway_instructions', lang)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌐 " + t('connect.connect_gateway', lang), url=auth_url)],
                    _back_button(lang),
                ])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[Connect] Gateway error for {chat_id}: {e}")
        await callback.message.edit_text(
            t('connect.gateway_unavailable', lang),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_back_button(lang)]),
        )


# ============= NAVIGATION =============

@connect_router.callback_query(F.data == "iwe_back")
async def on_back(callback: CallbackQuery):
    """Назад к списку клиентов."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    gateway, github = await _check_connections(chat_id)
    text = _build_status_text(lang, gateway, github)
    keyboard = _build_menu_keyboard(lang)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")


@connect_router.callback_query(F.data == "iwe_close")
async def on_close(callback: CallbackQuery):
    """Закрыть wizard."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    await callback.message.edit_text(t('connect.closed', lang), parse_mode="Markdown")


# ============= HELPERS =============

async def _check_connections(chat_id: int) -> tuple:
    """Проверить статус подключений. Returns (gateway, github)."""
    from clients.gateway_mcp import gateway_mcp
    gateway = gateway_mcp.is_connected(chat_id)

    from clients.github_oauth import github_oauth
    github = await github_oauth.is_connected(chat_id)

    return gateway, github
