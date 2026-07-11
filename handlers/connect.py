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
import os

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandObject

from config import GATEWAY_MCP_URL, ORY_CLIENT_ID
from db.queries import get_intern
from db.queries.external_clients import create_auth_code, list_client_tokens, revoke_client_token
from helpers.dual_write import resolve_ory_id_from_chat
from clients.ory_oauth import ory_oauth
from i18n import t

# Базовый URL бота (используется в инструкции по обмену кода)
_BOT_AUTH_BASE = os.getenv("WEBHOOK_URL", "").rstrip("/")

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


@connect_router.message(Command("connect_external"))
async def cmd_connect_external(message: Message):
    """Команда /connect_external — алиас на подключение внешнего клиента, для пункта меню бота."""
    await _handle_connect_external(message)


@connect_router.message(Command("connect"))
async def cmd_connect(message: Message, command: CommandObject):
    """Команда /connect — IWE setup wizard. /connect external — внешний клиент."""
    if command.args == "external":
        await _handle_connect_external(message)
        return

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


# ============= EXTERNAL CLIENT AUTH (WP-411 Ф2) =============

async def _handle_connect_external(message: Message):
    """Генерирует одноразовый код для подключения внешнего AI-клиента (напр. Claude Code)."""
    chat_id = message.chat.id
    logger.info(f"[ConnectExternal] START chat_id={chat_id}")

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        await message.answer(
            "⚠️ Твой аккаунт ещё не связан с платформой. "
            "Пройди онбординг и попробуй снова."
        )
        return

    try:
        code = await create_auth_code(chat_id, account_id)
    except Exception as exc:
        logger.error(f"[ConnectExternal] create_auth_code failed for chat_id={chat_id}: {exc}", exc_info=True)
        await message.answer("⚠️ Ошибка создания кода, попробуй позже.")
        return

    text = (
        "🔑 *Код подключения внешнего клиента*\n\n"
        f"`{code}`\n\n"
        "⏱ Действителен *5 минут*.\n\n"
        "*Шаги:*\n"
        "1. Скопируй код выше\n"
        "2. В терминале (из папки бота):\n"
        f"`python3 scripts/test_external_auth.py --code {code}`\n\n"
        "Увидишь галочки — всё работает и токен сохранён.\n\n"
        "Активные подключения: /my\\_clients"
    )
    try:
        await message.answer(text, parse_mode="Markdown")
    except Exception as exc:
        logger.error(f"[ConnectExternal] message.answer failed for chat_id={chat_id}: {exc}", exc_info=True)
        await message.answer(f"Код: `{code}` (5 мин)", parse_mode="Markdown")


@connect_router.message(Command("my_clients"))
async def cmd_my_clients(message: Message):
    """Список активных внешних клиентов с кнопками отзыва."""
    chat_id = message.chat.id

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        await message.answer("⚠️ Аккаунт не найден.")
        return

    tokens = await list_client_tokens(account_id)
    if not tokens:
        await message.answer(
            "Нет активных внешних подключений.\n\n"
            "Чтобы подключить внешний клиент: /connect external"
        )
        return

    lines = ["*Активные внешние подключения:*\n"]
    keyboard_rows = []
    for tok in tokens:
        since = tok["created_at"].strftime("%d.%m.%Y") if tok.get("created_at") else "?"
        last = tok["last_used"].strftime("%d.%m %H:%M") if tok.get("last_used") else "не использовался"
        lines.append(f"• *{tok['client_label']}* — с {since}, последний доступ: {last}")
        keyboard_rows.append([
            InlineKeyboardButton(
                text=f"❌ Отключить {tok['client_label']}",
                callback_data=f"iwe_revoke_{tok['id']}",
            )
        ])

    keyboard_rows.append([
        InlineKeyboardButton(text="✅ Закрыть", callback_data="iwe_close")
    ])
    text = "\n".join(lines)
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        parse_mode="Markdown",
    )


@connect_router.callback_query(F.data.startswith("iwe_revoke_"))
async def on_revoke_client(callback: CallbackQuery):
    """Отзывает токен внешнего клиента."""
    await callback.answer()
    token_id = callback.data.removeprefix("iwe_revoke_")

    account_id = await resolve_ory_id_from_chat(callback.from_user.id)
    if not account_id:
        await callback.message.edit_text("⚠️ Аккаунт не найден.")
        return

    revoked = await revoke_client_token(token_id, account_id)
    if revoked:
        await callback.message.edit_text(
            "✅ Подключение отозвано. Внешний клиент больше не имеет доступа."
        )
    else:
        await callback.message.edit_text(
            "⚠️ Не удалось отозвать — токен уже неактивен или не принадлежит тебе."
        )
