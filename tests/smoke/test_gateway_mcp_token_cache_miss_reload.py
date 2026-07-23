"""
WP-7 gateway_mcp fix (2026-07-23): cache-miss token lookup must fall back to
_refresh_single_token()'s DB reload (WP-200) instead of failing instantly.

Incident 2026-07-22 20:53:21 UTC: grant_consent returned None x4 in 1.4s for
user 409855567, right after a slow /start (8.8s, nav-latency alert). Account
resolved fine (persona.ory_identity, realtime DB lookup) but the in-memory
_tokens cache in gateway_mcp missed — _get_access_token() had no DB fallback,
unlike the reactive 401 path, so the call never even reached the gateway.
"""

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
