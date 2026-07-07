"""L3 health check: distinguish Railway auth errors from empty deployment lists."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.health_check import RailwayAuthError, _get_latest_deployment_id


def _mock_session(status: int, payload: dict):
    """Build a ClientSession mock whose post() returns `payload` as JSON."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value=f"mocked body for status {status}")

    post_cm = MagicMock()
    post_cm.__aenter__ = AsyncMock(return_value=resp)
    post_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=post_cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm


@pytest.mark.asyncio
async def test_graphql_errors_raise_auth_error():
    """Invalid/unauthorized token → GraphQL `errors` field → RailwayAuthError, not None."""
    payload = {"errors": [{"message": "Not Authorized"}], "data": None}
    with patch("core.health_check.aiohttp.ClientSession", return_value=_mock_session(200, payload)):
        with pytest.raises(RailwayAuthError):
            await _get_latest_deployment_id("bad-token", "svc-id", "env-id")


@pytest.mark.asyncio
async def test_empty_edges_returns_none_without_raising():
    """Valid token, genuinely no deployments → None, no exception."""
    payload = {"data": {"deployments": {"edges": []}}}
    with patch("core.health_check.aiohttp.ClientSession", return_value=_mock_session(200, payload)):
        result = await _get_latest_deployment_id("good-token", "svc-id", "env-id")
    assert result is None


@pytest.mark.asyncio
async def test_valid_deployment_returns_id():
    payload = {"data": {"deployments": {"edges": [{"node": {"id": "dep-123", "status": "SUCCESS"}}]}}}
    with patch("core.health_check.aiohttp.ClientSession", return_value=_mock_session(200, payload)):
        result = await _get_latest_deployment_id("good-token", "svc-id", "env-id")
    assert result == "dep-123"


@pytest.mark.asyncio
async def test_non_200_status_returns_none_without_raising():
    """HTTP-level failure (e.g. 500/502) -> None, not RailwayAuthError (that's for GraphQL-level auth errors)."""
    with patch("core.health_check.aiohttp.ClientSession", return_value=_mock_session(502, {})):
        result = await _get_latest_deployment_id("token", "svc-id", "env-id")
    assert result is None
