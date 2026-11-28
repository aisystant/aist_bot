from __future__ import annotations

"""
Хендлеры интеграции с GitHub (OAuth, заметки).

Команды:
- /github — подключение/статус/отключение
- /github disconnect — отключить
- Сообщения с "." — исчезающие заметки
"""

import logging
import os
import time

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command

from clients.github_auth import GitHubAuthUnavailable
from config import GITHUB_APP_NOTES_ENABLED
from core.fleeting_notes import NoteOutcome, NoteSource, NoteStatus, notes_blocker, write_note
from db.queries import get_intern
from db.queries.github_app import get_app_installation
from i18n import t
from integrations.telegram.keyboards import kb_note_undo

logger = logging.getLogger(__name__)

github_router = Router(name="github")

# Двусторонний pending: "." может прийти ДО или ПОСЛЕ forward
_pending_forwards: dict[int, tuple] = {}            # (comment|None, timestamp) → ждём forward
_pending_forward_messages: dict[int, tuple] = {}    # forward → ждём "."
_FORWARD_TTL_SECONDS = 60


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


async def _active_installation(telegram_user_id: int) -> dict | None:
    """Активная (не suspended) App-установка пользователя, если есть.

    Одна установка на пользователя обслуживает и заметки, и «Персональное
    руководство» (WP-301) — см. WP-406 Ф22.
    """
    installation = await get_app_installation(telegram_user_id)
    if installation and not installation.get("app_suspended"):
        return installation
    return None


async def _list_repo_choices(telegram_user_id: int, limit: int = 10) -> list[dict]:
    """Список репозиториев для выбора target/knowledge repo.

    App-установка есть → только репозитории, выбранные при установке App
    (repository_selection=selected, WP-458 ВЫ-13). Иначе — прежний путь
    через OAuth (все репозитории аккаунта, легаси на льготном периоде).
    """
    installation = await _active_installation(telegram_user_id)
    if installation:
        from clients.github_app import get_installation_repos
        repos = await get_installation_repos(installation["app_installation_id"])
        return repos[:limit]

    from clients.github_oauth import github_oauth
    return await github_oauth.get_repos(telegram_user_id, limit=limit) or []


async def _offer_oauth_connect(message: Message, telegram_user_id: int, lang: str) -> None:
    """Прежний путь подключения (OAuth scope "repo") — легаси и fallback при
    незавершённой настройке App (WP-406 Ф22)."""
    from clients.github_oauth import github_oauth

    try:
        auth_url, state = await github_oauth.get_authorization_url(telegram_user_id)

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
        try:
            result = await github_notes.clear_notes(telegram_user_id)
        except GitHubAuthUnavailable as e:
            logger.warning("clear_notes auth unavailable for chat_id=%d: %s", telegram_user_id, e)
            await message.answer(t('github.reconnect_required', lang))
            return
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
        knowledge_repo = await github_oauth.get_knowledge_repo(telegram_user_id)

        status_lines = [
            f"*{t('github.connected_title', lang)}*\n",
            f"{t('github.user_label', lang)}: *{login}*",
        ]

        buttons = []

        if target_repo:
            status_lines.append(f"{t('github.repo_label', lang)}: `{target_repo}`")
            status_lines.append(f"{t('github.path_label', lang)}: `{notes_path}`")
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

        if knowledge_repo:
            status_lines.append(f"{t('github.knowledge_repo_label', lang)}: `{knowledge_repo}`")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=t('github.btn_change_knowledge_repo', lang),
                        callback_data="github_select_knowledge_repo",
                    )
                ]
            )
        else:
            status_lines.append(f"\n{t('github.no_knowledge_repo', lang)}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=t('github.btn_select_knowledge_repo', lang),
                        callback_data="github_select_knowledge_repo",
                    )
                ]
            )

        if target_repo:
            status_lines.append(f"\n{t('github.note_instruction', lang)}")
            status_lines.append(t('github.note_example', lang))

        from core.access import access_layer
        has_notes_access = await access_layer.has_access(telegram_user_id, 'notes')
        if not has_notes_access:
            status_lines.append(f"\n{t('github.sub_required_hint', lang)}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=t('aisystant_sub.btn_subscribe', lang),
                        callback_data="aisystant_subscribe",
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
    elif GITHUB_APP_NOTES_ENABLED and not os.getenv("GITHUB_APP_SLUG", "").strip():
        # Флаг включён, но App ещё не зарегистрирован платформой — тихий
        # откат на OAuth ниже воспроизвёл бы ровно ту проблему (WP-458
        # ВЫ-13), ради которой флаг вообще включали. Логируем явно, иначе
        # никто не узнает, почему новые пользователи всё ещё видят OAuth.
        logger.warning(
            "[GitHubApp] GITHUB_APP_NOTES_ENABLED=true, но GITHUB_APP_SLUG пуст — "
            "cmd_github откатывается на OAuth для chat_id=%d", telegram_user_id,
        )
        await _offer_oauth_connect(message, telegram_user_id, lang)
    elif GITHUB_APP_NOTES_ENABLED:
        # WP-406 Ф22: новые подключения идут через GitHub App
        # (repository_selection=selected) вместо OAuth scope "repo".
        # Уже подключённые по OAuth продолжают работать без изменений —
        # флаг не трогает существующие подключения (peer-сессия раунд 2).
        base_url = os.getenv("WEBHOOK_URL", "").rstrip("/")
        if not base_url:
            await message.answer(t('github.config_error', lang, error="WEBHOOK_URL"))
            return
        install_url = f"{base_url}/auth/github_app/setup?telegram_user_id={telegram_user_id}"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t('github.btn_connect', lang), url=install_url)]
            ]
        )
        await message.answer(
            f"*{t('github.connect_title', lang)}*\n\n"
            f"{t('github.connect_app_desc', lang)}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    else:
        await _offer_oauth_connect(message, telegram_user_id, lang)


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

    installation = await _active_installation(telegram_user_id)
    repos = await _list_repo_choices(telegram_user_id, limit=10)
    if not repos:
        if installation:
            await callback.message.edit_text(t('github.app_repos_empty', lang))
        else:
            await callback.message.edit_text(t('github.repos_error', lang))
        return

    buttons = []
    for repo in repos:
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

    await callback.message.edit_text(
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
    knowledge_repo = await github_oauth.get_knowledge_repo(telegram_user_id)

    await callback.answer(t('github.repo_selected', lang), show_alert=True)

    text = (
        f"*{t('github.repo_configured', lang)}*\n\n"
        f"{t('github.repo_label', lang)}: `{repo_full_name}`\n"
        f"{t('github.path_label', lang)}: `{notes_path}`\n\n"
        f"{t('github.repo_configured_desc', lang)}\n"
        f"{t('github.note_example', lang)}\n\n"
        f"{t('github.note_will_be_saved', lang, repo=repo_full_name, path=notes_path)}"
    )

    buttons = []
    if not knowledge_repo:
        text += f"\n\n{t('github.next_step_knowledge_repo', lang)}"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=t('github.btn_select_knowledge_repo', lang),
                    callback_data="github_select_knowledge_repo",
                )
            ]
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@github_router.callback_query(F.data == "github_select_knowledge_repo")
async def callback_github_select_knowledge_repo(callback: CallbackQuery):
    """Показывает список репозиториев для выбора индекса знаний."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not await github_oauth.is_connected(telegram_user_id):
        await callback.answer(t('github.not_connected_alert', lang), show_alert=True)
        return

    await callback.answer()

    installation = await _active_installation(telegram_user_id)
    repos = await _list_repo_choices(telegram_user_id, limit=10)
    if not repos:
        if installation:
            await callback.message.edit_text(t('github.app_repos_empty', lang))
        else:
            await callback.message.edit_text(t('github.repos_error', lang))
        return

    buttons = []
    for repo in repos:
        full_name = repo.get("full_name", "")
        name = repo.get("name", "")
        buttons.append(
            [
                InlineKeyboardButton(
                    text=name,
                    callback_data=f"github_knowledge_repo:{full_name}",
                )
            ]
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(
        f"*{t('github.select_knowledge_repo_title', lang)}*\n\n"
        f"{t('github.select_knowledge_repo_desc', lang)}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@github_router.callback_query(F.data.startswith("github_knowledge_repo:"))
async def callback_github_knowledge_repo_selected(callback: CallbackQuery):
    """Обработка выбора репозитория индекса знаний."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)
    repo_full_name = callback.data.split(":", 1)[1]

    await github_oauth.set_knowledge_repo(telegram_user_id, repo_full_name)
    target_repo = await github_oauth.get_target_repo(telegram_user_id)

    await callback.answer(t('github.knowledge_repo_selected', lang), show_alert=True)

    notes_line = f"{t('github.repo_label', lang)}: `{target_repo}`\n" if target_repo else ""
    await callback.message.edit_text(
        f"✅ *{t('github.knowledge_repo_configured', lang)}*\n\n"
        f"{notes_line}"
        f"{t('github.knowledge_repo_label', lang)}: `{repo_full_name}`\n\n"
        f"{t('github.all_configured', lang)}",
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


_NOTE_BLOCKER_REPLIES = {
    NoteStatus.NO_GITHUB: 'github.note_needs_github',
    NoteStatus.NO_REPO: 'github.repo_not_selected',
}


async def _answer_blocker(message: Message, status: NoteStatus, lang: str) -> None:
    """Tell the user why the note was not saved (no access, no GitHub, no notes repo)."""
    if status is NoteStatus.NO_ACCESS:
        from core.access import access_layer

        text, keyboard = await access_layer.get_paywall('notes', lang)
        await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
        return
    await message.answer(t(_NOTE_BLOCKER_REPLIES[status], lang))


async def _answer_written(message: Message, outcome: NoteOutcome, lang: str, source: NoteSource) -> None:
    """Answer after a write attempt; a voice note also gets an undo button (recognition can be wrong)."""
    if outcome.status is NoteStatus.SAVED:
        keyboard = kb_note_undo(outcome.undo_ref) if source is NoteSource.VOICE and outcome.undo_ref else None
        await message.answer(t('github.note_saved', lang, url=outcome.url), reply_markup=keyboard)
    elif outcome.status is NoteStatus.RECONNECT:
        await message.answer(t('github.reconnect_required', lang))
    else:
        await message.answer(t('github.note_error', lang))


@github_router.message(F.text.startswith("."))
async def handle_fleeting_note(message: Message):
    """Обработка исчезающих заметок: набранный текст, начатый с точки."""
    await process_fleeting_note(message, NoteSource.TEXT)


async def process_fleeting_note(
    message: Message, source: NoteSource, *, operation_id=None, target=None, created_at=None, bind_block_hash=None, queue_retry=True,
) -> NoteOutcome | None:
    """Заметка из сообщения, текст которого начинается с точки (набранного или надиктованного).

    Сценарии:
    A. reply на сообщение (с текстом после точки или без) — объединить, записать
    B. forward → "." или ".текст" — объединить forwarded + комментарий, записать
    C. "." (голая точка) → forward — ждать пересылку (TTL), записать forwarded
    D. ".текст" (без reply/forward) — записать немедленно
    """
    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    blocker = await notes_blocker(telegram_user_id)
    if blocker is not None:
        await _answer_blocker(message, blocker, lang)
        return

    note_text = (message.text or "")[1:].strip()

    # Сценарий A: reply на сообщение (с текстом после точки или без)
    if message.reply_to_message:
        replied_text = _extract_message_text(message.reply_to_message, lang)
        if replied_text:
            note_text = f"Мой комментарий: {note_text}\n\n{replied_text}" if note_text else replied_text

    # Сценарий B: forward → "." (проверяем ожидающую пересылку)
    merged_with_forward = False
    if not message.reply_to_message:
        pending = _pending_forward_messages.pop(telegram_user_id, None)
        if pending:
            fwd_msg, fwd_time = pending
            if (time.time() - fwd_time) <= _FORWARD_TTL_SECONDS:
                fwd_text = _extract_message_text(fwd_msg, lang)
                if fwd_text:
                    note_text = f"Мой комментарий: {note_text}\n\n{fwd_text}" if note_text else fwd_text
                    merged_with_forward = True

    # Сценарий C: голая "." без reply/forward → ожидать пересылку
    # Сценарий D: ".текст" без reply/forward → сохранить немедленно (не ждать forward)
    if not message.reply_to_message and not merged_with_forward:
        if not note_text:
            _pending_forwards[telegram_user_id] = (None, time.time())
            return

    write_options = {}
    if operation_id is not None or target is not None or created_at is not None or bind_block_hash is not None or not queue_retry:
        write_options = {"operation_id": operation_id, "target": target, "created_at": created_at, "bind_block_hash": bind_block_hash, "queue_retry": queue_retry}
    outcome = await write_note(
        telegram_user_id, note_text, source, message_id=message.message_id, **write_options,
    )
    await _answer_written(message, outcome, lang, source)
    return outcome


@github_router.message(F.forward_origin | F.forward_date)
async def handle_forwarded_message(message: Message):
    """Обработка пересланных сообщений → заметки."""
    telegram_user_id = message.chat.id

    # Вариант 1: "." или ". комментарий" → forward (pending точка существует)
    pending_info = _pending_forwards.pop(telegram_user_id, None)
    if pending_info:
        pending_comment, pending_time = pending_info
        if (time.time() - pending_time) <= _FORWARD_TTL_SECONDS:
            if await notes_blocker(telegram_user_id) is not None:
                return

            intern = await get_intern(telegram_user_id)
            lang = _lang(intern)
            fwd_text = _extract_message_text(message, lang)
            if not fwd_text and not pending_comment:
                await message.answer(t('github.no_text', lang))
                return

            if pending_comment and fwd_text:
                note_text = f"Мой комментарий: {pending_comment}\n\n{fwd_text}"
            else:
                note_text = pending_comment or fwd_text

            outcome = await write_note(telegram_user_id, note_text, NoteSource.TEXT, message_id=message.message_id)
            await _answer_written(message, outcome, lang, NoteSource.TEXT)
            return

    # Вариант 2: forward → "." (сохраняем forward, ждём точку)
    _pending_forward_messages[telegram_user_id] = (message, time.time())


def _extract_message_text(message: Message, lang: str = 'ru') -> str:
    """Извлекает текст из сообщения (обычного или пересланного)."""
    parts = []

    # Sender attribution: new API (forward_origin) → deprecated fields fallback
    sender_name = None
    if message.forward_origin:
        origin = message.forward_origin
        if hasattr(origin, 'sender_user') and origin.sender_user:
            sender_name = origin.sender_user.full_name
        elif hasattr(origin, 'sender_user_name') and origin.sender_user_name:
            sender_name = origin.sender_user_name
        elif hasattr(origin, 'chat') and origin.chat:
            sender_name = origin.chat.title or origin.chat.full_name
    elif message.forward_from:
        sender_name = message.forward_from.full_name
    elif message.forward_sender_name:
        sender_name = message.forward_sender_name

    if sender_name:
        parts.append(t('github.from_user', lang, name=sender_name))

    if message.text:
        parts.append(message.text)
    elif message.caption:
        parts.append(message.caption)

    return " ".join(parts).strip()
