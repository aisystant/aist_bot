"""
Тесты WP-406 Ф22: /github (заметки) на GitHub App с repository_selection=selected.

Peer-сессия 2026-09-10-08 (Kimi+Codex): shared installation с «Персональным
руководством» (WP-301) — disconnect заметок не должен трогать App-установку.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db.queries.github import disconnect_github_notes
from states.utilities.mydata import MyDataState


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Мок asyncpg-соединения: fetchrow (FOR UPDATE) для has_app, execute — запись команды."""

    def __init__(self, has_app: bool):
        self._installation_id = 42 if has_app else None
        self.executed = []

    def transaction(self):
        return _FakeTransaction()

    async def fetchrow(self, query, *args):
        return {"app_installation_id": self._installation_id}

    async def execute(self, query, *args):
        self.executed.append((query, args))


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_disconnect_notes_with_app_installation_preserves_it():
    """При наличии App-установки disconnect делает UPDATE (не DELETE строки)."""
    conn = _FakeConn(has_app=True)
    with patch("db.queries.github.get_secrets_pool", new_callable=AsyncMock) as mock_pool:
        mock_pool.return_value = _FakePool(conn)
        await disconnect_github_notes(12345)

    assert len(conn.executed) == 1
    query, args = conn.executed[0]
    assert "UPDATE" in query
    assert "DELETE" not in query
    assert "app_installation_id" not in query  # App-поля не упоминаются — не трогаем их
    assert args == (12345,)


@pytest.mark.asyncio
async def test_disconnect_notes_without_app_installation_deletes_row():
    """Без App-установки поведение как до Ф22 — полное удаление строки."""
    conn = _FakeConn(has_app=False)
    with patch("db.queries.github.get_secrets_pool", new_callable=AsyncMock) as mock_pool:
        mock_pool.return_value = _FakePool(conn)
        await disconnect_github_notes(12345)

    assert len(conn.executed) == 1
    query, args = conn.executed[0]
    assert "DELETE" in query
    assert args == (12345,)


@pytest.mark.asyncio
async def test_mydata_disconnect_github_uses_safe_path_not_full_delete():
    """/mydata → «Отключить GitHub» шёл в обход клиента напрямую на
    delete_github_connection (нашёл /verify smoke, вторая находка сессии) —
    тот же риск для shared App-установки, что и в handlers/github.py."""
    state = MyDataState(bot=None, db=None, llm=None, i18n=None)
    state.send = AsyncMock()
    user = {"chat_id": 555, "language": "ru"}

    with patch("db.queries.github.disconnect_github_notes", new_callable=AsyncMock) as mock_disconnect, \
         patch("db.queries.github.delete_github_connection", new_callable=AsyncMock) as mock_delete:
        await state._disconnect_github(user)

    mock_disconnect.assert_awaited_once_with(555)
    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_github_offers_app_install_when_flag_enabled_and_not_connected():
    """Новое подключение при включённом флаге ведёт на install App, не на OAuth authorize."""
    from handlers.github import cmd_github

    message = MagicMock()
    message.text = "/github"
    message.chat.id = 999
    message.answer = AsyncMock()

    oauth = MagicMock()
    oauth.is_connected = AsyncMock(return_value=False)

    with patch("handlers.github.get_intern", new_callable=AsyncMock) as mock_get_intern, \
         patch("clients.github_oauth.github_oauth", oauth), \
         patch("handlers.github.GITHUB_APP_NOTES_ENABLED", True), \
         patch.dict("os.environ", {"GITHUB_APP_SLUG": "aisystant-personal-guide",
                                    "WEBHOOK_URL": "https://bot.example.com"}):
        mock_get_intern.return_value = {"language": "ru"}
        await cmd_github(message)

    keyboard = message.answer.call_args.kwargs.get("reply_markup") or message.answer.call_args[1]["reply_markup"]
    install_url = keyboard.inline_keyboard[0][0].url
    assert install_url == "https://bot.example.com/auth/github_app/setup?telegram_user_id=999"


@pytest.mark.asyncio
async def test_cmd_github_falls_back_to_oauth_when_flag_enabled_but_slug_missing():
    """Флаг включён, но GITHUB_APP_SLUG не задан — откат на OAuth, не тихий."""
    from handlers.github import cmd_github

    message = MagicMock()
    message.text = "/github"
    message.chat.id = 999
    message.answer = AsyncMock()

    oauth = MagicMock()
    oauth.is_connected = AsyncMock(return_value=False)
    oauth.get_authorization_url = AsyncMock(return_value=("https://github.com/login/oauth/authorize?...", "state"))

    with patch("handlers.github.get_intern", new_callable=AsyncMock) as mock_get_intern, \
         patch("clients.github_oauth.github_oauth", oauth), \
         patch("handlers.github.GITHUB_APP_NOTES_ENABLED", True), \
         patch("handlers.github.logger") as mock_logger, \
         patch.dict("os.environ", {"GITHUB_APP_SLUG": ""}, clear=False):
        mock_get_intern.return_value = {"language": "ru"}
        await cmd_github(message)

    oauth.get_authorization_url.assert_awaited_once()
    mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_github_falls_back_to_oauth_when_flag_disabled():
    """Флаг выключен (дефолт) — поведение не меняется, идёт OAuth authorize URL."""
    from handlers.github import cmd_github

    message = MagicMock()
    message.text = "/github"
    message.chat.id = 999
    message.answer = AsyncMock()

    oauth = MagicMock()
    oauth.is_connected = AsyncMock(return_value=False)
    oauth.get_authorization_url = AsyncMock(return_value=("https://github.com/login/oauth/authorize?...", "state"))

    with patch("handlers.github.get_intern", new_callable=AsyncMock) as mock_get_intern, \
         patch("clients.github_oauth.github_oauth", oauth), \
         patch("handlers.github.GITHUB_APP_NOTES_ENABLED", False):
        mock_get_intern.return_value = {"language": "ru"}
        await cmd_github(message)

    oauth.get_authorization_url.assert_awaited_once()
