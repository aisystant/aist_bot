"""
IWE Connect wizard — инструкции подключения AI-клиентов (WP-209 Ф1).

Команда /connect показывает как подключить AI-ассистент (claude.ai,
Cursor, ChatGPT, Claude Code) к знаниям платформы через Gateway MCP.

НЕ дублирует Settings → Подключения (OAuth-подключения бота).
/connect = инструкции для ВНЕШНИХ AI-клиентов.
Settings = управление подключениями БОТА (Gateway, GitHub, WakaTime, Calendar).

Связь с WP-199: после реализации нового онбординга (Ф3)
кнопка «Подключить IWE» вызовет этот wizard.
"""

import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from config import GATEWAY_MCP_URL, ORY_CLIENT_ID
from db.queries import get_intern
from helpers.dual_write import resolve_ory_id_from_chat
from clients.ory_oauth import ory_oauth
from i18n import t

logger = logging.getLogger(__name__)

connect_router = Router(name="connect")


# ============= MAIN MENU =============

def _build_menu_text(lang: str) -> str:
    """Текст главного экрана wizard."""
    return (
        f"*{t('connect.title', lang)}*\n\n"
        f"{t('connect.subtitle', lang)}\n\n"
        f"{t('connect.choose_client', lang)}"
    )


def _build_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора клиента."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Claude (claude.ai)", callback_data="iwe_claude")],
        [InlineKeyboardButton(text="⌨️ Cursor / Windsurf / Cline", callback_data="iwe_cursor")],
        [InlineKeyboardButton(text="💬 ChatGPT", callback_data="iwe_chatgpt")],
        [InlineKeyboardButton(text="🖥 Claude Code (полный IWE)", callback_data="iwe_claude_code")],
        [InlineKeyboardButton(text=t('connect.done', lang), callback_data="iwe_close")],
    ])


async def _build_t0_gate_message(chat_id: int) -> tuple[str | None, InlineKeyboardMarkup | None]:
    """Возвращает (text, keyboard) если пользователь T0 (без Ory-аккаунта), иначе (None, None)."""
    if not ORY_CLIENT_ID:
        return None, None
    account_id = await resolve_ory_id_from_chat(chat_id)
    if account_id:
        return None, None
    auth_url, _ = await ory_oauth.get_authorization_url(chat_id)
    text = (
        "Перед подключением AI-клиентов нужно создать аккаунт на платформе Aisystant.\n\n"
        "Нажми кнопку ниже — введи email и придумай пароль. Займёт 30 секунд.\n\n"
        "После регистрации снова открой /connect — появятся инструкции по подключению."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Зарегистрироваться на Aisystant", url=auth_url)],
    ])
    return text, keyboard


@connect_router.message(Command("connect"))
async def cmd_connect(message: Message):
    """Команда /connect — IWE setup wizard."""
    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    if not intern:
        await message.answer(t('connect.need_start', lang))
        return

    t0_text, t0_keyboard = await _build_t0_gate_message(message.chat.id)
    if t0_text:
        await message.answer(t0_text, reply_markup=t0_keyboard)
        return

    lang = intern.get('language', 'ru') or 'ru'
    text = _build_menu_text(lang)
    keyboard = _build_menu_keyboard(lang)
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


@connect_router.callback_query(F.data == "iwe_connect_start")
async def on_connect_start(callback: CallbackQuery):
    """Точка входа из onboarding (inline-кнопка после /start)."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    if not intern:
        return

    t0_text, t0_keyboard = await _build_t0_gate_message(callback.from_user.id)
    if t0_text:
        await callback.message.edit_text(t0_text, reply_markup=t0_keyboard)
        return

    lang = intern.get('language', 'ru') or 'ru'
    text = _build_menu_text(lang)
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
            url="https://claude.ai/customize/connectors",
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


@connect_router.callback_query(F.data == "iwe_claude_code")
async def on_claude_code(callback: CallbackQuery):
    """Инструкция подключения Claude Code (полный IWE)."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    text = t('connect.claude_code_instructions', lang, gateway_url=GATEWAY_MCP_URL)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        _back_button(lang),
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ============= NAVIGATION =============

@connect_router.callback_query(F.data == "iwe_back")
async def on_back(callback: CallbackQuery):
    """Назад к списку клиентов."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'

    text = _build_menu_text(lang)
    keyboard = _build_menu_keyboard(lang)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")


@connect_router.callback_query(F.data == "iwe_close")
async def on_close(callback: CallbackQuery):
    """Закрыть wizard."""
    await callback.answer()
    intern = await get_intern(callback.from_user.id)
    lang = intern.get('language', 'ru') or 'ru' if intern else 'ru'
    await callback.message.edit_text(t('connect.closed', lang), parse_mode="Markdown")
