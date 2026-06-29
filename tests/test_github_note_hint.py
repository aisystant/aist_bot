"""
Регрессионные тесты: подсказка "точка в начале" для заметок в GitHub.

WP-7 BUGTRIAGE1: пользователь не понимал, как отправлять заметки из бота,
потому что инструкция показывалась не везде.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from handlers.github import cmd_github
from states.common.settings import SettingsState


NOTE_INSTRUCTION_RU = "Отправьте сообщение с точкой в начале"
NOTE_EXAMPLE_RU = ".купить книгу по СМ"


def _make_github_oauth(target_repo: str | None = "owner/notes", knowledge_repo: str | None = None):
    """Мок OAuth-клиента GitHub с нужным статусом подключения."""
    oauth = MagicMock()
    oauth.is_connected = AsyncMock(return_value=True)
    oauth.get_user = AsyncMock(return_value={"login": "testuser"})
    oauth.get_target_repo = AsyncMock(return_value=target_repo)
    oauth.set_target_repo = AsyncMock()
    oauth.get_knowledge_repo = AsyncMock(return_value=knowledge_repo)
    oauth.get_notes_path = AsyncMock(return_value="inbox/fleeting-notes.md")
    oauth.get_strategy_repo = AsyncMock(return_value=None)
    return oauth


def _make_message(text: str = "/github", chat_id: int = 12345):
    message = MagicMock()
    message.text = text
    message.chat.id = chat_id
    message.answer = AsyncMock()
    return message


def _make_callback(chat_id: int = 12345, user_id: int = 12345):
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.message.chat.id = chat_id
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _make_user(chat_id: int = 12345, language: str = "ru"):
    return {"chat_id": chat_id, "language": language, "current_context": {}}


@pytest.mark.asyncio
async def test_github_command_shows_note_hint_when_only_target_repo():
    """/github: подсказка видна, даже если не выбрано репо знаний."""
    message = _make_message()
    oauth = _make_github_oauth(target_repo="owner/notes", knowledge_repo=None)

    with patch("handlers.github.get_intern", new_callable=AsyncMock) as mock_get_intern, \
         patch("clients.github_oauth.github_oauth", oauth), \
         patch("core.access.access_layer") as mock_access:
        mock_get_intern.return_value = {"language": "ru"}
        mock_access.has_access = AsyncMock(return_value=True)

        await cmd_github(message)

    text = message.answer.call_args[0][0]
    assert NOTE_INSTRUCTION_RU in text, f"Ожидалась подсказка в ответе: {text!r}"
    assert NOTE_EXAMPLE_RU in text, f"Ожидался пример в ответе: {text!r}"


@pytest.mark.asyncio
async def test_settings_github_status_shows_note_hint_when_target_repo():
    """Настройки → GitHub: подсказка видна, если выбран репозиторий заметок."""
    state = SettingsState(bot=None, db=None, llm=None, i18n=None)
    callback = _make_callback()
    user = _make_user()
    oauth = _make_github_oauth(target_repo="owner/notes", knowledge_repo=None)

    with patch("clients.github_oauth.github_oauth", oauth):
        await state._handle_github_connection(user, callback)

    text = callback.message.edit_text.call_args[0][0]
    assert NOTE_INSTRUCTION_RU in text, f"Ожидалась подсказка в статусе: {text!r}"
    assert NOTE_EXAMPLE_RU in text, f"Ожидался пример в статусе: {text!r}"


@pytest.mark.asyncio
async def test_settings_github_repo_selected_shows_note_hint():
    """Настройки → выбор репозитория заметок: сразу показываем инструкцию."""
    state = SettingsState(bot=None, db=None, llm=None, i18n=None)
    callback = _make_callback()
    user = _make_user()
    oauth = _make_github_oauth(target_repo="owner/notes", knowledge_repo=None)

    with patch("clients.github_oauth.github_oauth", oauth):
        await state._github_repo_selected(user, callback, "github_repo:owner/notes")

    text = callback.message.edit_text.call_args[0][0]
    assert NOTE_INSTRUCTION_RU in text, f"Ожидалась подсказка после выбора репо: {text!r}"
    assert NOTE_EXAMPLE_RU in text, f"Ожидался пример после выбора репо: {text!r}"
