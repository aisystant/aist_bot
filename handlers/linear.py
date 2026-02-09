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

logger = logging.getLogger(__name__)

linear_router = Router(name="linear")


@linear_router.message(Command("linear"))
async def cmd_linear(message: Message):
    """Команда для интеграции с Linear.

    Подкоманды:
    - /linear — показать статус и получить ссылку авторизации
    - /linear tasks — показать свои задачи
    - /linear disconnect — отключить интеграцию
    """
    from clients.linear_oauth import linear_oauth

    intern = await get_intern(message.chat.id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    telegram_user_id = message.chat.id

    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    subcommand = parts[1].lower() if len(parts) > 1 else None

    is_connected = linear_oauth.is_connected(telegram_user_id)

    if subcommand == "disconnect":
        if is_connected:
            linear_oauth.disconnect(telegram_user_id)
            await message.answer("✅ Linear отключён.")
        else:
            await message.answer("ℹ️ Linear не был подключён.")
        return

    if subcommand == "tasks":
        if not is_connected:
            await message.answer(
                "❌ Linear не подключён.\n\n"
                "Используйте /linear для подключения."
            )
            return

        await message.answer("⏳ Загружаю задачи...")
        issues = await linear_oauth.get_my_issues(telegram_user_id, limit=10)

        if issues is None:
            await message.answer("❌ Не удалось получить задачи. Попробуйте переподключиться: /linear disconnect, затем /linear")
            return

        if not issues:
            await message.answer("📭 У вас нет назначенных задач.")
            return

        lines = ["📋 *Ваши задачи в Linear:*\n"]
        for issue in issues:
            state_name = issue.get("state", {}).get("name", "?")
            identifier = issue.get("identifier", "?")
            title = issue.get("title", "Без названия")
            url = issue.get("url", "")

            lines.append(f"• [{identifier}]({url}) — {title}\n  _{state_name}_")

        await message.answer("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)
        return

    # Основная команда /linear — показать статус или ссылку авторизации
    if is_connected:
        viewer = await linear_oauth.get_viewer(telegram_user_id)
        name = viewer.get("name", "пользователь") if viewer else "пользователь"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои задачи", callback_data="linear_tasks")],
            [InlineKeyboardButton(text="🔌 Отключить Linear", callback_data="linear_disconnect")]
        ])

        await message.answer(
            f"✅ *Linear подключён*\n\n"
            f"Вы авторизованы как: *{name}*",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        try:
            auth_url, state = linear_oauth.get_authorization_url(telegram_user_id)

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Подключить Linear", url=auth_url)]
            ])

            await message.answer(
                "🔗 *Подключение к Linear*\n\n"
                "Нажмите кнопку ниже, чтобы авторизоваться в Linear.\n\n"
                "После авторизации вы сможете видеть свои задачи прямо в боте.",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except ValueError as e:
            await message.answer(
                f"❌ Ошибка конфигурации: {e}\n\n"
                "Обратитесь к администратору."
            )


@linear_router.callback_query(F.data == "linear_tasks")
async def callback_linear_tasks(callback: CallbackQuery):
    """Callback для кнопки 'Мои задачи' Linear."""
    try:
        from clients.linear_oauth import linear_oauth
    except ImportError:
        await callback.answer("Linear интеграция не настроена", show_alert=True)
        return

    telegram_user_id = callback.from_user.id

    if not linear_oauth.is_connected(telegram_user_id):
        await callback.answer("Linear не подключён", show_alert=True)
        return

    await callback.answer()

    issues = await linear_oauth.get_my_issues(telegram_user_id, limit=10)

    if not issues:
        await callback.message.answer("📋 В Linear пока нет задач.")
        return

    lines = ["📋 *Задачи Linear:*\n"]
    for issue in issues:
        state_name = issue.get("state", {}).get("name", "?")
        identifier = issue.get("identifier", "?")
        title = issue.get("title", "Без названия")
        url = issue.get("url", "")

        lines.append(f"• [{identifier}]({url}) — {title}\n  _{state_name}_")

    await callback.message.answer("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


@linear_router.callback_query(F.data == "linear_disconnect")
async def callback_linear_disconnect(callback: CallbackQuery):
    """Callback для кнопки 'Отключить Linear'."""
    try:
        from clients.linear_oauth import linear_oauth
    except ImportError:
        await callback.answer("Linear интеграция не настроена", show_alert=True)
        return

    telegram_user_id = callback.from_user.id

    if not linear_oauth.is_connected(telegram_user_id):
        await callback.answer("Linear уже отключён", show_alert=True)
        return

    linear_oauth.disconnect(telegram_user_id)
    await callback.answer("Linear отключён", show_alert=True)

    await callback.message.edit_text(
        "🔌 *Linear отключён*\n\n"
        "Используйте /linear чтобы подключиться снова.",
        parse_mode="Markdown"
    )
