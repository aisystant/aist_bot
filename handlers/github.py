"""
Хендлеры интеграции с GitHub (OAuth, заметки).

Команды:
- /github — подключение/статус/отключение
- /github disconnect — отключить
- Сообщения с "." — исчезающие заметки
"""

import logging
import time

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

github_router = Router(name="github")

# Ожидание пересылки после ".": user_id -> timestamp (TTL 60 сек)
_pending_forwards: dict[int, float] = {}


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@github_router.message(Command("github"))
async def cmd_github(message: Message):
    """Команда /github — подключение, статус, отключение."""
    from clients.github_oauth import github_oauth

    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)
    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    subcommand = parts[1].lower() if len(parts) > 1 else None

    is_connected = await github_oauth.is_connected(telegram_user_id)

    if subcommand == "disconnect":
        if is_connected:
            await github_oauth.disconnect(telegram_user_id)
            await message.answer(t('github.disconnected', lang))
        else:
            await message.answer(t('github.not_connected', lang))
        return

    if subcommand == "clear":
        if not is_connected:
            await message.answer(t('github.not_connected_cmd', lang))
            return
        target_repo = await github_oauth.get_target_repo(telegram_user_id)
        if not target_repo:
            await message.answer(t('github.repo_not_selected', lang))
            return
        from clients.github_api import github_notes
        result = await github_notes.clear_notes(telegram_user_id)
        if result:
            await message.answer(t('github.notes_cleared', lang))
        else:
            await message.answer(t('github.notes_clear_error', lang))
        return

    if is_connected:
        user_info = await github_oauth.get_user(telegram_user_id)
        login = user_info.get("login", "user") if user_info else "user"
        target_repo = await github_oauth.get_target_repo(telegram_user_id)
        notes_path = await github_oauth.get_notes_path(telegram_user_id)

        status_lines = [
            f"*{t('github.connected_title', lang)}*\n",
            f"{t('github.user_label', lang)}: *{login}*",
        ]

        buttons = []

        if target_repo:
            status_lines.append(f"{t('github.repo_label', lang)}: `{target_repo}`")
            status_lines.append(f"{t('github.path_label', lang)}: `{notes_path}`")
            status_lines.append(f"\n{t('github.note_instruction', lang)}")
            status_lines.append(t('github.note_example', lang))
        else:
            status_lines.append(f"\n{t('github.no_repo', lang)}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=t('github.btn_select_repo', lang),
                        callback_data="github_select_repo",
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=t('github.btn_disconnect', lang),
                    callback_data="github_disconnect",
                )
            ]
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await message.answer(
            "\n".join(status_lines),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    else:
        try:
            auth_url, state = github_oauth.get_authorization_url(telegram_user_id)

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=t('github.btn_connect', lang), url=auth_url)]
                ]
            )

            await message.answer(
                f"*{t('github.connect_title', lang)}*\n\n"
                f"{t('github.connect_desc', lang)}",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except ValueError as e:
            await message.answer(t('github.config_error', lang, error=str(e)))


@github_router.callback_query(F.data == "github_select_repo")
async def callback_github_select_repo(callback: CallbackQuery):
    """Показывает список репозиториев для выбора."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not await github_oauth.is_connected(telegram_user_id):
        await callback.answer(t('github.not_connected_alert', lang), show_alert=True)
        return

    await callback.answer()

    repos = await github_oauth.get_repos(telegram_user_id, limit=20)
    if not repos:
        await callback.message.answer(t('github.repos_error', lang))
        return

    buttons = []
    for repo in repos[:10]:
        full_name = repo.get("full_name", "")
        name = repo.get("name", "")
        buttons.append(
            [
                InlineKeyboardButton(
                    text=name,
                    callback_data=f"github_repo:{full_name}",
                )
            ]
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.answer(
        f"*{t('github.select_repo_title', lang)}*\n\n"
        f"{t('github.select_repo_desc', lang)}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@github_router.callback_query(F.data.startswith("github_repo:"))
async def callback_github_repo_selected(callback: CallbackQuery):
    """Обработка выбора репозитория."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)
    repo_full_name = callback.data.split(":", 1)[1]

    await github_oauth.set_target_repo(telegram_user_id, repo_full_name)
    notes_path = await github_oauth.get_notes_path(telegram_user_id)

    await callback.answer(t('github.repo_selected', lang), show_alert=True)

    await callback.message.edit_text(
        f"*{t('github.repo_configured', lang)}*\n\n"
        f"{t('github.repo_label', lang)}: `{repo_full_name}`\n"
        f"{t('github.path_label', lang)}: `{notes_path}`\n\n"
        f"{t('github.repo_configured_desc', lang)}\n"
        f"{t('github.note_example', lang)}\n\n"
        f"{t('github.note_will_be_saved', lang, repo=repo_full_name, path=notes_path)}",
        parse_mode="Markdown",
    )


@github_router.callback_query(F.data == "github_disconnect")
async def callback_github_disconnect(callback: CallbackQuery):
    """Отключение GitHub."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not await github_oauth.is_connected(telegram_user_id):
        await callback.answer(t('github.already_disconnected', lang), show_alert=True)
        return

    await github_oauth.disconnect(telegram_user_id)
    await callback.answer(t('github.disconnected_alert', lang), show_alert=True)

    await callback.message.edit_text(
        f"*{t('github.disconnected', lang)}*\n\n"
        f"{t('github.reconnect_hint', lang)}",
        parse_mode="Markdown",
    )


@github_router.message(F.text.startswith("."))
async def handle_fleeting_note(message: Message):
    """Обработка исчезающих заметок.

    Сценарии:
    1. ".текст" — записать текст как заметку
    2. "." (reply на сообщение) — записать текст из replied сообщения
    3. "." (без reply) — ожидать пересылку (следующее сообщение)
    """
    from clients.github_oauth import github_oauth
    from clients.github_api import github_notes

    telegram_user_id = message.chat.id

    # Проверяем доступ (подписка/триал)
    from core.access import access_layer
    if not await access_layer.has_access(telegram_user_id, 'notes'):
        return

    if not await github_oauth.is_connected(telegram_user_id):
        return
    target_repo = await github_oauth.get_target_repo(telegram_user_id)
    if not target_repo:
        return

    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)
    note_text = (message.text or "")[1:].strip()

    # Сценарий 2: "." как reply на сообщение
    if not note_text and message.reply_to_message:
        replied = message.reply_to_message
        note_text = _extract_message_text(replied, lang)
        if not note_text:
            return

    # Сценарий 3: просто "." — ожидать пересылку (тихо, без сообщения)
    if not note_text:
        _pending_forwards[telegram_user_id] = time.time()
        return

    # Сценарий 1 и 2: записываем
    result = await github_notes.append_note(telegram_user_id, note_text)

    if result:
        url = f"https://github.com/{result['repo']}/blob/main/{result['path']}"
        await message.answer(t('github.note_saved', lang, url=url))
    else:
        await message.answer(t('github.note_error', lang))


@github_router.message(F.forward_date)
async def handle_forwarded_message(message: Message):
    """Обработка пересланных сообщений → заметки."""
    from clients.github_oauth import github_oauth
    from clients.github_api import github_notes

    telegram_user_id = message.chat.id

    pending_time = _pending_forwards.get(telegram_user_id)
    if not pending_time or (time.time() - pending_time) > 3:
        return

    del _pending_forwards[telegram_user_id]

    if not await github_oauth.is_connected(telegram_user_id):
        return
    target_repo = await github_oauth.get_target_repo(telegram_user_id)
    if not target_repo:
        return

    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)
    note_text = _extract_message_text(message, lang)
    if not note_text:
        await message.answer(t('github.no_text', lang))
        return

    result = await github_notes.append_note(telegram_user_id, note_text)

    if result:
        url = f"https://github.com/{result['repo']}/blob/main/{result['path']}"
        await message.answer(t('github.note_saved', lang, url=url))
    else:
        await message.answer(t('github.note_error', lang))


def _extract_message_text(message: Message, lang: str = 'ru') -> str:
    """Извлекает текст из сообщения (обычного или пересланного)."""
    parts = []

    if message.forward_from:
        parts.append(t('github.from_user', lang, name=message.forward_from.full_name))
    elif message.forward_sender_name:
        parts.append(t('github.from_user', lang, name=message.forward_sender_name))

    if message.text:
        parts.append(message.text)
    elif message.caption:
        parts.append(message.caption)

    return " ".join(parts).strip()
