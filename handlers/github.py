"""
Хендлеры интеграции с GitHub (OAuth, заметки).

Команды:
- /github — подключение/статус/отключение
- /github disconnect — отключить
- Сообщения с "." — исчезающие заметки
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

logger = logging.getLogger(__name__)

github_router = Router(name="github")


@github_router.message(Command("github"))
async def cmd_github(message: Message):
    """Команда /github — подключение, статус, отключение."""
    from clients.github_oauth import github_oauth

    telegram_user_id = message.chat.id
    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    subcommand = parts[1].lower() if len(parts) > 1 else None

    is_connected = await github_oauth.is_connected(telegram_user_id)

    if subcommand == "disconnect":
        if is_connected:
            await github_oauth.disconnect(telegram_user_id)
            await message.answer("GitHub отключён.")
        else:
            await message.answer("GitHub не был подключён.")
        return

    if subcommand == "clear":
        if not is_connected:
            await message.answer("GitHub не подключён. /github")
            return
        target_repo = await github_oauth.get_target_repo(telegram_user_id)
        if not target_repo:
            await message.answer("Репозиторий не выбран. /github")
            return
        from clients.github_api import github_notes
        result = await github_notes.clear_notes(telegram_user_id)
        if result:
            await message.answer("Заметки очищены.")
        else:
            await message.answer("Не удалось очистить заметки.")
        return

    if is_connected:
        user_info = await github_oauth.get_user(telegram_user_id)
        login = user_info.get("login", "user") if user_info else "user"
        target_repo = await github_oauth.get_target_repo(telegram_user_id)
        notes_path = await github_oauth.get_notes_path(telegram_user_id)

        status_lines = [
            f"*GitHub подключён*\n",
            f"Пользователь: *{login}*",
        ]

        buttons = []

        if target_repo:
            status_lines.append(f"Репо для заметок: `{target_repo}`")
            status_lines.append(f"Путь: `{notes_path}`")
            status_lines.append(
                f"\nОтправьте сообщение с точкой в начале, чтобы записать заметку:"
            )
            status_lines.append(f"`.купить книгу по СМ`")
        else:
            status_lines.append("\nРепозиторий для заметок не выбран.")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Выбрать репо", callback_data="github_select_repo"
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    text="Отключить GitHub", callback_data="github_disconnect"
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
                    [InlineKeyboardButton(text="Подключить GitHub", url=auth_url)]
                ]
            )

            await message.answer(
                "*Подключение к GitHub*\n\n"
                "Нажмите кнопку ниже, чтобы авторизоваться в GitHub.\n\n"
                "После авторизации вы сможете записывать исчезающие заметки "
                "прямо из Telegram в свой репозиторий.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except ValueError as e:
            await message.answer(f"Ошибка конфигурации: {e}")


@github_router.callback_query(F.data == "github_select_repo")
async def callback_github_select_repo(callback: CallbackQuery):
    """Показывает список репозиториев для выбора."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id

    if not await github_oauth.is_connected(telegram_user_id):
        await callback.answer("GitHub не подключён", show_alert=True)
        return

    await callback.answer()

    repos = await github_oauth.get_repos(telegram_user_id, limit=20)
    if not repos:
        await callback.message.answer("Не удалось получить список репозиториев.")
        return

    # Показываем кнопки для каждого репо
    buttons = []
    for repo in repos[:10]:  # Ограничиваем 10 кнопками
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
        "*Выберите репозиторий для заметок:*\n\n"
        "Заметки будут записываться в файл `inbox/fleeting-notes.md`",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@github_router.callback_query(F.data.startswith("github_repo:"))
async def callback_github_repo_selected(callback: CallbackQuery):
    """Обработка выбора репозитория."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id
    repo_full_name = callback.data.split(":", 1)[1]

    await github_oauth.set_target_repo(telegram_user_id, repo_full_name)

    notes_path = await github_oauth.get_notes_path(telegram_user_id)

    await callback.answer("Репозиторий выбран!", show_alert=True)

    await callback.message.edit_text(
        f"*Репозиторий настроен!*\n\n"
        f"Репо: `{repo_full_name}`\n"
        f"Путь: `{notes_path}`\n\n"
        f"Теперь отправляйте сообщения с точкой в начале:\n"
        f"`.купить книгу по СМ`\n\n"
        f"Заметка будет записана в `{repo_full_name}/{notes_path}`",
        parse_mode="Markdown",
    )


@github_router.callback_query(F.data == "github_disconnect")
async def callback_github_disconnect(callback: CallbackQuery):
    """Отключение GitHub."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id

    if not await github_oauth.is_connected(telegram_user_id):
        await callback.answer("GitHub уже отключён", show_alert=True)
        return

    await github_oauth.disconnect(telegram_user_id)
    await callback.answer("GitHub отключён", show_alert=True)

    await callback.message.edit_text(
        "*GitHub отключён*\n\n"
        "Используйте /github чтобы подключиться снова.",
        parse_mode="Markdown",
    )


@github_router.message(F.text.startswith("."))
async def handle_fleeting_note(message: Message):
    """Обработка исчезающих заметок (сообщения с '.' в начале)."""
    from clients.github_oauth import github_oauth
    from clients.github_api import github_notes

    telegram_user_id = message.chat.id

    # Проверяем подключение
    if not await github_oauth.is_connected(telegram_user_id):
        return  # Тихо пропускаем — пользователь может не знать о фиче

    # Проверяем, что выбран репо
    target_repo = await github_oauth.get_target_repo(telegram_user_id)
    if not target_repo:
        return  # Тихо пропускаем

    # Извлекаем текст заметки (без точки)
    note_text = message.text[1:].strip()
    if not note_text:
        return  # Пустая заметка

    # Записываем
    result = await github_notes.append_note(telegram_user_id, note_text)

    if result:
        repo = result["repo"]
        path = result["path"]
        url = f"https://github.com/{repo}/blob/main/{path}"
        await message.answer(f"Записано → {url}")
    else:
        await message.answer("Не удалось записать заметку. Проверьте /github")
