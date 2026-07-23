"""
WP-7 gateway_mcp fix (2026-07-23): cache-miss token lookup must fall back to
_refresh_single_token()'s DB reload (WP-200) instead of failing instantly.

Incident 2026-07-22 20:53:21 UTC: grant_consent returned None x4 in 1.4s for
user 409855567, right after a slow /start (8.8s, nav-latency alert). Account
resolved fine (persona.ory_identity, realtime DB lookup) but the in-memory
_tokens cache in gateway_mcp missed — _get_access_token() had no DB fallback,
unlike the reactive 401 path, so the call never even reached the gateway.
"""

import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from clients.gateway_mcp import GatewayMCPClient


def _mock_response(status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {"result": {"ok": True}})
    return resp


class _PostCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_do_call_reloads_token_on_cache_miss():
    client = GatewayMCPClient(url="http://gateway.test")
    user_id = 409855567

    resp = _mock_response(json_data={"result": {"success": True}})
    session = MagicMock()
    session.post = MagicMock(return_value=_PostCtx(resp))

    async def fake_refresh(uid):
        client._tokens[uid] = {
            "access_token": "fresh-token",
            "refresh_token": "r",
            "expires_at": None,
            "ory_id": "x",
        }
        return True

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)), \
         patch.object(client, "_refresh_single_token", new=AsyncMock(side_effect=fake_refresh)) as mock_refresh:
        result = await client._do_call("grant_consent", {"agreed": True}, telegram_user_id=user_id)

    mock_refresh.assert_called_once_with(user_id)
    assert result == {"success": True}
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh-token"


@pytest.mark.asyncio
async def test_do_call_still_fails_closed_when_no_token_in_db_either():
    client = GatewayMCPClient(url="http://gateway.test")
    user_id = 999999999

    session = MagicMock()
    session.post = MagicMock()

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)), \
         patch.object(client, "_refresh_single_token", new=AsyncMock(return_value=False)) as mock_refresh:
        result = await client._do_call("grant_consent", {"agreed": True}, telegram_user_id=user_id)

    mock_refresh.assert_called_once_with(user_id)
    assert result is None
    session.post.assert_not_called()


@pytest.mark.asyncio
async def test_do_call_fast_path_skips_refresh_when_token_cached():
    """Hot path (token already in memory) must not touch _refresh_single_token
    at all — the fix only changes the cache-miss branch."""
    client = GatewayMCPClient(url="http://gateway.test")
    user_id = 777888999
    client._tokens[user_id] = {
        "access_token": "cached-token", "refresh_token": "r",
        "expires_at": None, "ory_id": "x",
    }

    resp = _mock_response(json_data={"result": {"ok": True}})
    session = MagicMock()
    session.post = MagicMock(return_value=_PostCtx(resp))

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)), \
         patch.object(client, "_refresh_single_token", new=AsyncMock()) as mock_refresh:
        result = await client._do_call("grant_consent", {"agreed": True}, telegram_user_id=user_id)

    mock_refresh.assert_not_called()
    assert result == {"ok": True}
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer cached-token"


@pytest.mark.asyncio
async def test_do_call_skips_refresh_within_negative_cache_ttl():
    """Second call for a never-linked (T0) user within NO_TOKEN_CACHE_TTL must not
    hit _refresh_single_token again — routine unlinked-user traffic (e.g.
    collect_pre_search on every question) must not spam load_one_ory_token()
    or the gateway_token_cache_miss metric that feeds a real Grafana alert
    (>10/hour, docs/processes/process-10-gateway-mcp.md)."""
    client = GatewayMCPClient(url="http://gateway.test")
    user_id = 111222333

    session = MagicMock()
    session.post = MagicMock()

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)), \
         patch.object(client, "_refresh_single_token", new=AsyncMock(return_value=False)) as mock_refresh:
        first = await client._do_call("knowledge_search", {}, telegram_user_id=user_id)
        second = await client._do_call("knowledge_search", {}, telegram_user_id=user_id)

    assert first is None
    assert second is None
    mock_refresh.assert_called_once_with(user_id)
    session.post.assert_not_called()


@pytest.mark.asyncio
async def test_do_call_retries_refresh_after_negative_cache_ttl_expires():
    client = GatewayMCPClient(url="http://gateway.test")
    user_id = 444555666

    resp = _mock_response(json_data={"result": {"ok": True}})
    session = MagicMock()
    session.post = MagicMock(return_value=_PostCtx(resp))

    async def fake_refresh(uid):
        client._tokens[uid] = {
            "access_token": "late-token", "refresh_token": "r",
            "expires_at": None, "ory_id": "x",
        }
        return True

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)), \
         patch.object(client, "_refresh_single_token", new=AsyncMock(side_effect=fake_refresh)) as mock_refresh:
        client._no_token_users[user_id] = time.time() - client.NO_TOKEN_CACHE_TTL - 1
        result = await client._do_call("grant_consent", {"agreed": True}, telegram_user_id=user_id)

    mock_refresh.assert_called_once_with(user_id)
    assert result == {"ok": True}
    assert user_id not in client._no_token_users


@pytest.mark.asyncio
async def test_negative_cache_timestamp_does_not_slide_within_ttl():
    """Found by independent code-review verification (2026-07-23): a naive
    implementation overwrites no_token_since on every falsy-token call,
    including the cache-hit-skip branch — turning the fixed 60s window into
    a sliding one that never expires under continuous traffic from one user.
    A call still inside the TTL must not touch the stored timestamp at all,
    and must not attempt _refresh_single_token again."""
    client = GatewayMCPClient(url="http://gateway.test")
    user_id = 222333444
    first_failure_ts = time.time() - 10  # 10s into the 60s window
    client._no_token_users[user_id] = first_failure_ts

    session = MagicMock()
    session.post = MagicMock()

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)), \
         patch.object(client, "_refresh_single_token", new=AsyncMock(return_value=False)) as mock_refresh:
        result = await client._do_call("knowledge_search", {}, telegram_user_id=user_id)

    assert result is None
    mock_refresh.assert_not_called()
    assert client._no_token_users[user_id] == first_failure_ts
