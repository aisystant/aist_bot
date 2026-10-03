"""WP-5 Ф58.4: a refused account-link decision must leave both link and tokens untouched."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import oauth_server
from clients import ory_link_guard
from clients.ory_link_guard import (
    ExistingOryAccount,
    OryLinkApproval,
    OryLinkGuardUnavailable,
    VerifiedEmailRequired,
)


SUB = "11111111-2222-3333-4444-555555555555"


async def callback_with_guard(verdict, *, link_result=True, current_sub=SUB):
    request = MagicMock()
    request.query = {"code": "code", "state": "state"}
    guard = AsyncMock(side_effect=verdict if isinstance(verdict, Exception) else None)
    if not isinstance(verdict, Exception):
        guard.return_value = verdict
    link, save_tokens, gateway = AsyncMock(return_value=link_result), AsyncMock(), MagicMock()

    @asynccontextmanager
    async def locked(_chat_id):
        yield MagicMock(fetchval=AsyncMock(return_value=current_sub))

    with patch("config.settings.ORY_LINK_GUARD_ENABLED", True), \
            patch.object(oauth_server.ory_oauth, "validate_state", AsyncMock(return_value=111)), \
            patch.object(oauth_server.ory_oauth, "exchange_code", AsyncMock(return_value={"access_token": "a", "refresh_token": "r"})), \
            patch.object(oauth_server.ory_oauth, "get_userinfo", AsyncMock(return_value={"sub": SUB, "email": "unverified@example.org"})), \
            patch("clients.ory_link_guard.check_ory_link", guard), \
            patch.object(oauth_server, "_ory_link_write_lock", locked), \
            patch("db.queries.identity.link_ory", link), \
            patch("db.queries.ory_tokens.save_ory_tokens", save_tokens), \
            patch("clients.gateway_mcp.gateway_mcp", gateway), \
            patch("db.connection.get_pool", AsyncMock(side_effect=RuntimeError("test db unavailable"))), \
            patch.object(oauth_server, "_bot_instance", None):
        response = await oauth_server.ory_callback_handler(request)
    return response, guard, link, save_tokens, gateway


@pytest.mark.parametrize(
    ("verdict", "status", "message"),
    [
        (ExistingOryAccount(), 409, "прежний аккаунт"),
        (VerifiedEmailRequired(), 422, "Подтвердите почту"),
        (OryLinkGuardUnavailable("down"), 503, "Попробуйте позже"),
    ],
)
async def test_guard_refusal_never_writes_link_or_tokens(verdict, status, message):
    response, guard, link, save_tokens, gateway = await callback_with_guard(verdict)
    assert response.status == status
    assert message in response.text
    guard.assert_awaited_once_with("a", SUB)
    link.assert_not_awaited()
    save_tokens.assert_not_awaited()
    gateway.set_tokens.assert_not_called()


async def test_guard_approval_uses_verified_email_instead_of_userinfo_email():
    approved = OryLinkApproval(sub=SUB, email="verified@example.org")
    response, _, link, save_tokens, gateway = await callback_with_guard(approved)
    assert response.status == 200
    link.assert_awaited_once_with(111, SUB, "verified@example.org")
    save_tokens.assert_awaited_once()
    gateway.set_tokens.assert_called_once()


async def test_failed_link_after_approval_does_not_store_tokens():
    approved = OryLinkApproval(sub=SUB, email="verified@example.org")
    response, _, link, save_tokens, gateway = await callback_with_guard(approved, link_result=False)
    assert response.status == 409
    link.assert_awaited_once()
    save_tokens.assert_not_awaited()
    gateway.set_tokens.assert_not_called()


async def test_changed_link_does_not_store_tokens():
    approved = OryLinkApproval(sub=SUB, email="verified@example.org")
    response, _, link, save_tokens, gateway = await callback_with_guard(
        approved, current_sub="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    assert response.status == 409
    link.assert_awaited_once()
    save_tokens.assert_not_awaited()
    gateway.set_tokens.assert_not_called()


class _HttpReply:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.payload


class _HttpSession:
    def __init__(self, reply):
        self.reply = reply
        self.headers = None
        self.allow_redirects = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def post(self, _, *, headers, allow_redirects):
        self.headers = headers
        self.allow_redirects = allow_redirects
        return self.reply


async def test_client_sends_both_credentials_and_checks_subject(monkeypatch):
    monkeypatch.setattr(ory_link_guard, "USER_PROFILE_SERVICE_URL", "https://profile.example")
    monkeypatch.setattr(ory_link_guard, "BOT_LINK_GUARD_SECRET", "bot-secret")
    session = _HttpSession(_HttpReply(200, {"allowed": True, "sub": SUB, "email": "verified@example.org"}))
    with patch.object(ory_link_guard.aiohttp, "ClientSession", return_value=session):
        approval = await ory_link_guard.check_ory_link("user-token", SUB)
    assert approval.email == "verified@example.org"
    assert session.headers == {
        "Authorization": "Bearer user-token",
        "X-Bot-Link-Guard-Secret": "bot-secret",
    }
    assert session.allow_redirects is False

    session.reply = _HttpReply(200, {"allowed": True, "sub": "different-sub", "email": None})
    with patch.object(ory_link_guard.aiohttp, "ClientSession", return_value=session):
        with pytest.raises(OryLinkGuardUnavailable):
            await ory_link_guard.check_ory_link("user-token", SUB)


async def test_client_rejects_redirect_without_forwarding_credentials(monkeypatch):
    monkeypatch.setattr(ory_link_guard, "USER_PROFILE_SERVICE_URL", "https://profile.example")
    monkeypatch.setattr(ory_link_guard, "BOT_LINK_GUARD_SECRET", "bot-secret")
    session = _HttpSession(_HttpReply(302, {}))
    with patch.object(ory_link_guard.aiohttp, "ClientSession", return_value=session):
        with pytest.raises(OryLinkGuardUnavailable):
            await ory_link_guard.check_ory_link("user-token", SUB)
    assert session.allow_redirects is False


async def test_client_rejects_missing_secret_without_sending_user_token(monkeypatch):
    monkeypatch.setattr(ory_link_guard, "USER_PROFILE_SERVICE_URL", "https://profile.example")
    monkeypatch.setattr(ory_link_guard, "BOT_LINK_GUARD_SECRET", "")
    with patch.object(ory_link_guard.aiohttp, "ClientSession") as session:
        with pytest.raises(OryLinkGuardUnavailable):
            await ory_link_guard.check_ory_link("user-token", SUB)
    session.assert_not_called()
