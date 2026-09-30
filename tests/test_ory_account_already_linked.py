"""Ory callback: the Ory account is already linked to another Telegram row.

Found in prod on 2026-09-30: one person had two Telegram rows in public.users; the second
one tried to take the same ory_id, link_ory hit users_ory_id_key and the callback returned a
raw 500. The user must get a clear message with the support contact instead.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

import oauth_server
from db.queries import identity


def _unique_violation(constraint: str) -> asyncpg.UniqueViolationError:
    return asyncpg.UniqueViolationError.new({"C": "23505", "n": constraint, "M": "duplicate key"})


def _pool_raising(exc: Exception):
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=exc)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


async def test_link_ory_raises_dedicated_error_on_ory_id_conflict():
    pool = _pool_raising(_unique_violation("users_ory_id_key"))
    with patch.object(identity, "get_pool", AsyncMock(return_value=pool)):
        with pytest.raises(identity.OryAccountAlreadyLinked):
            await identity.link_ory(111, "433fd7f9-d914-4c47-88b3-a37bbe964040", None)


async def test_link_ory_keeps_other_unique_violations_untouched():
    pool = _pool_raising(_unique_violation("users_email_key"))
    with patch.object(identity, "get_pool", AsyncMock(return_value=pool)):
        with pytest.raises(asyncpg.UniqueViolationError):
            await identity.link_ory(111, "433fd7f9-d914-4c47-88b3-a37bbe964040", None)


async def test_already_linked_response_is_409_with_support_contact_and_telegram_message():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch.object(oauth_server, "_bot_instance", bot):
        resp = await oauth_server._ory_already_linked_response(111)
    assert resp.status == 409
    assert "@ssm_tg" in resp.text
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 111
    assert "@ssm_tg" in bot.send_message.await_args.kwargs["text"]


async def test_already_linked_response_survives_failed_telegram_send():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    with patch.object(oauth_server, "_bot_instance", bot):
        resp = await oauth_server._ory_already_linked_response(111)
    assert resp.status == 409


async def test_callback_handler_returns_409_instead_of_500_when_ory_id_is_taken():
    request = MagicMock()
    request.query = {"code": "c", "state": "s"}
    save_tokens, gateway = AsyncMock(), MagicMock()
    link = AsyncMock(side_effect=identity.OryAccountAlreadyLinked(111))
    with patch.object(oauth_server.ory_oauth, "validate_state", AsyncMock(return_value=111)), \
            patch.object(oauth_server.ory_oauth, "exchange_code", AsyncMock(return_value={"access_token": "a", "refresh_token": "r"})), \
            patch.object(oauth_server.ory_oauth, "get_userinfo", AsyncMock(return_value={"sub": "433fd7f9-d914-4c47-88b3-a37bbe964040"})), \
            patch("db.queries.ory_tokens.save_ory_tokens", save_tokens), \
            patch("clients.gateway_mcp.gateway_mcp", gateway), \
            patch("db.queries.identity.link_ory", link), \
            patch.object(oauth_server, "_bot_instance", None):
        resp = await oauth_server.ory_callback_handler(request)
    assert resp.status == 409
    assert "@ssm_tg" in resp.text
    link.assert_awaited_once()
    # the losing Telegram row must not keep the other row's Ory tokens
    save_tokens.assert_not_awaited()
    gateway.set_tokens.assert_not_called()


async def test_callback_handler_still_succeeds_and_saves_tokens_when_link_works():
    request = MagicMock()
    request.query = {"code": "c", "state": "s"}
    save_tokens, gateway = AsyncMock(), MagicMock()
    link = AsyncMock(return_value=True)
    with patch.object(oauth_server.ory_oauth, "validate_state", AsyncMock(return_value=111)), \
            patch.object(oauth_server.ory_oauth, "exchange_code", AsyncMock(return_value={"access_token": "a", "refresh_token": "r"})), \
            patch.object(oauth_server.ory_oauth, "get_userinfo", AsyncMock(return_value={"sub": "433fd7f9-d914-4c47-88b3-a37bbe964040"})), \
            patch("db.queries.ory_tokens.save_ory_tokens", save_tokens), \
            patch("clients.gateway_mcp.gateway_mcp", gateway), \
            patch("db.queries.identity.link_ory", link), \
            patch("db.connection.get_pool", AsyncMock(side_effect=RuntimeError("no db in test"))), \
            patch.object(oauth_server, "_bot_instance", None):
        resp = await oauth_server.ory_callback_handler(request)
    assert resp.status == 200
    link.assert_awaited_once()
    save_tokens.assert_awaited_once()
    gateway.set_tokens.assert_called_once()
