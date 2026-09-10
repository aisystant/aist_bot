"""
Тесты для единой точки выбора авторизации GitHub (WP-406 Ф22).

Приоритет: активная App-установка > OAuth в льготном периоде > отказ.
Peer-сессия 2026-09-10-08 (Kimi+Codex): проверить приоритет источника,
границы льготного периода, отсутствие подключения.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from clients.github_auth import (
    GitHubAuthUnavailable,
    OperationClass,
    resolve_auth_context,
)


def _date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_active_installation_wins_over_oauth():
    """Активная App-установка используется даже если OAuth тоже подключён."""
    with patch("db.queries.github_app.get_app_installation", new_callable=AsyncMock) as mock_installation, \
         patch("clients.github_app.get_installation_token", new_callable=AsyncMock) as mock_token, \
         patch("clients.github_oauth.github_oauth") as mock_oauth:
        mock_installation.return_value = {"app_installation_id": 42, "app_suspended": False}
        mock_token.return_value = "installation-token-abc"
        mock_oauth.get_access_token = AsyncMock(return_value="oauth-token-should-not-be-used")

        ctx = await resolve_auth_context(123, OperationClass.WRITE)

        assert ctx.source == "app"
        assert ctx.token == "installation-token-abc"
        assert ctx.installation_id == 42
        mock_oauth.get_access_token.assert_not_called()


@pytest.mark.asyncio
async def test_suspended_installation_falls_back_to_oauth():
    """Suspended-установка не используется — WRITE идёт через OAuth (без grace-дедлайна)."""
    with patch("db.queries.github_app.get_app_installation", new_callable=AsyncMock) as mock_installation, \
         patch("clients.github_oauth.github_oauth") as mock_oauth, \
         patch.dict("os.environ", {"GITHUB_APP_OAUTH_GRACE_UNTIL": ""}):
        mock_installation.return_value = {"app_installation_id": 42, "app_suspended": True}
        mock_oauth.get_access_token = AsyncMock(return_value="oauth-token")

        ctx = await resolve_auth_context(123, OperationClass.WRITE)

        assert ctx.source == "oauth"
        assert ctx.token == "oauth-token"


@pytest.mark.asyncio
async def test_installation_token_mint_failure_does_not_fall_back_to_oauth():
    """Сбой выдачи installation token НЕ откатывается на OAuth молча (обошло бы selected-scope)."""
    with patch("db.queries.github_app.get_app_installation", new_callable=AsyncMock) as mock_installation, \
         patch("clients.github_app.get_installation_token", new_callable=AsyncMock) as mock_token, \
         patch("clients.github_oauth.github_oauth") as mock_oauth:
        mock_installation.return_value = {"app_installation_id": 42, "app_suspended": False}
        mock_token.return_value = None
        mock_oauth.get_access_token = AsyncMock(return_value="oauth-token")

        with pytest.raises(GitHubAuthUnavailable):
            await resolve_auth_context(123, OperationClass.WRITE)

        mock_oauth.get_access_token.assert_not_called()


@pytest.mark.asyncio
async def test_no_connection_at_all_raises():
    with patch("db.queries.github_app.get_app_installation", new_callable=AsyncMock) as mock_installation, \
         patch("clients.github_oauth.github_oauth") as mock_oauth:
        mock_installation.return_value = None
        mock_oauth.get_access_token = AsyncMock(return_value=None)

        with pytest.raises(GitHubAuthUnavailable):
            await resolve_auth_context(123, OperationClass.WRITE)


@pytest.mark.asyncio
async def test_oauth_read_allowed_regardless_of_grace_deadline():
    """READ через OAuth не проверяет grace-период — только WRITE."""
    past_deadline = _date(datetime.now(timezone.utc) - timedelta(days=2))
    with patch("db.queries.github_app.get_app_installation", new_callable=AsyncMock) as mock_installation, \
         patch("clients.github_oauth.github_oauth") as mock_oauth, \
         patch.dict("os.environ", {"GITHUB_APP_OAUTH_GRACE_UNTIL": past_deadline}):
        mock_installation.return_value = None
        mock_oauth.get_access_token = AsyncMock(return_value="oauth-token")

        ctx = await resolve_auth_context(123, OperationClass.READ)

        assert ctx.source == "oauth"


@pytest.mark.asyncio
async def test_oauth_write_blocked_after_grace_deadline():
    """Дедлайн включителен по конец дня (UTC) — берём позавчера, чтобы не зависеть от текущего часа."""
    past_deadline = _date(datetime.now(timezone.utc) - timedelta(days=2))
    with patch("db.queries.github_app.get_app_installation", new_callable=AsyncMock) as mock_installation, \
         patch("clients.github_oauth.github_oauth") as mock_oauth, \
         patch.dict("os.environ", {"GITHUB_APP_OAUTH_GRACE_UNTIL": past_deadline}):
        mock_installation.return_value = None
        mock_oauth.get_access_token = AsyncMock(return_value="oauth-token")

        with pytest.raises(GitHubAuthUnavailable):
            await resolve_auth_context(123, OperationClass.WRITE)


@pytest.mark.asyncio
async def test_oauth_write_allowed_on_deadline_day_itself():
    """Дедлайн = сегодняшняя дата — WRITE ещё разрешён (включительно до конца дня UTC)."""
    today = _date(datetime.now(timezone.utc))
    with patch("db.queries.github_app.get_app_installation", new_callable=AsyncMock) as mock_installation, \
         patch("clients.github_oauth.github_oauth") as mock_oauth, \
         patch.dict("os.environ", {"GITHUB_APP_OAUTH_GRACE_UNTIL": today}):
        mock_installation.return_value = None
        mock_oauth.get_access_token = AsyncMock(return_value="oauth-token")

        ctx = await resolve_auth_context(123, OperationClass.WRITE)

        assert ctx.source == "oauth"


@pytest.mark.asyncio
async def test_oauth_write_allowed_before_grace_deadline():
    future_deadline = _date(datetime.now(timezone.utc) + timedelta(days=1))
    with patch("db.queries.github_app.get_app_installation", new_callable=AsyncMock) as mock_installation, \
         patch("clients.github_oauth.github_oauth") as mock_oauth, \
         patch.dict("os.environ", {"GITHUB_APP_OAUTH_GRACE_UNTIL": future_deadline}):
        mock_installation.return_value = None
        mock_oauth.get_access_token = AsyncMock(return_value="oauth-token")

        ctx = await resolve_auth_context(123, OperationClass.WRITE)

        assert ctx.source == "oauth"


def test_auth_header_format_differs_by_source():
    """App использует 'token', OAuth — 'Bearer' (проверенные в проде форматы каждого источника)."""
    from clients.github_auth import AuthContext

    app_ctx = AuthContext(source="app", token="abc", installation_id=1)
    oauth_ctx = AuthContext(source="oauth", token="xyz")

    assert app_ctx.auth_header == "token abc"
    assert oauth_ctx.auth_header == "Bearer xyz"
