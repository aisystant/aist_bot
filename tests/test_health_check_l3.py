"""L3 health check: distinguish Railway auth errors from empty deployment lists."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.health_check import RailwayAuthError, _get_latest_deployment_id, _restart_deployment


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
    session_cm.session = session  # exposed for header assertions
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


@pytest.mark.asyncio
async def test_uses_project_access_token_header():
    """Project tokens (the type provisioned for this bot) authenticate via
    Project-Access-Token, not Authorization: Bearer - regression test for the
    incident where both bot services got "Not Authorized" despite a valid token."""
    payload = {"data": {"deployments": {"edges": [{"node": {"id": "dep-1", "status": "SUCCESS"}}]}}}
    session_cm = _mock_session(200, payload)
    with patch("core.health_check.aiohttp.ClientSession", return_value=session_cm):
        await _get_latest_deployment_id("proj-token", "svc-id", "env-id")

    _, kwargs = session_cm.session.post.call_args
    assert kwargs["headers"]["Project-Access-Token"] == "proj-token"
    assert "Authorization" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_restart_uses_project_access_token_header():
    """Same header requirement applies to the deploymentRestart mutation call site,
    which builds its own headers dict independently of _get_latest_deployment_id."""
    session_cm = _mock_session(200, {"data": {"deploymentRestart": True}})
    with patch("core.health_check.aiohttp.ClientSession", return_value=session_cm):
        success = await _restart_deployment("proj-token", "dep-123")

    assert success is True
    _, kwargs = session_cm.session.post.call_args
    assert kwargs["headers"]["Project-Access-Token"] == "proj-token"
    assert "Authorization" not in kwargs["headers"]
