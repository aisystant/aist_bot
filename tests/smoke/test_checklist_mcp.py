"""
Клиент checklist-mcp (WP-522 §3в) — standalone read-model чек-листа участника.
"""

import logging

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from clients.checklist_mcp import ChecklistMCPClient


def _mock_response(status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    return resp


class _PostCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _client_with_mocked_post(resp):
    client = ChecklistMCPClient(url="http://checklist-mcp.test", service_token="secret-token", timeout=3)
    session = MagicMock()
    session.post = MagicMock(return_value=_PostCtx(resp))
    patcher = patch.object(client, "_get_session", new=AsyncMock(return_value=session))
    patcher.start()
    return client, session


@pytest.mark.asyncio
async def test_get_checklist_status_returns_content_on_200():
    resp = _mock_response(json_data={"content": {"summary": {"current_confirmed_stage": "Г"}}})
    client, session = _client_with_mocked_post(resp)

    result = await client.get_checklist_status("acct-1")

    assert result == {"summary": {"current_confirmed_stage": "Г"}}
    called_url, = session.post.call_args.args
    assert called_url == "http://checklist-mcp.test/mcp/tools/call"
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer secret-token"
    assert session.post.call_args.kwargs["json"]["arguments"]["target_account_id"] == "acct-1"


@pytest.mark.asyncio
async def test_get_checklist_status_returns_none_when_content_missing():
    """200, но тело без ключа content (сервис ответил, форма неожиданная) — тоже None."""
    resp = _mock_response(json_data={})
    client, _ = _client_with_mocked_post(resp)

    result = await client.get_checklist_status("acct-1")

    assert result is None


@pytest.mark.asyncio
async def test_get_checklist_status_returns_none_on_403():
    resp = _mock_response(status=403, json_data={"error": "access denied"})
    client, _ = _client_with_mocked_post(resp)

    result = await client.get_checklist_status("acct-1")

    assert result is None


@pytest.mark.asyncio
async def test_get_checklist_status_returns_none_on_network_error():
    client = ChecklistMCPClient(url="http://checklist-mcp.test", service_token="secret-token", timeout=3)
    session = MagicMock()

    class _RaisingCtx:
        async def __aenter__(self):
            import aiohttp
            raise aiohttp.ClientConnectionError("connection refused")

        async def __aexit__(self, *exc):
            return False

    session.post = MagicMock(return_value=_RaisingCtx())
    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)):
        result = await client.get_checklist_status("acct-1")

    assert result is None


@pytest.mark.asyncio
async def test_get_checklist_status_skips_call_when_not_configured():
    client = ChecklistMCPClient(url="", service_token="", timeout=3)

    result = await client.get_checklist_status("acct-1")

    assert result is None


def test_is_configured_requires_both_url_and_token():
    assert not ChecklistMCPClient(url="", service_token="x", timeout=3).is_configured()
    assert not ChecklistMCPClient(url="http://x", service_token="", timeout=3).is_configured()
    assert ChecklistMCPClient(url="http://x", service_token="y", timeout=3).is_configured()


@pytest.mark.asyncio
async def test_get_checklist_status_logs_reason_on_failure(caplog):
    resp = _mock_response(status=500, json_data={"error": "internal error"})
    client, _ = _client_with_mocked_post(resp)

    with caplog.at_level(logging.WARNING):
        result = await client.get_checklist_status("acct-1")

    assert result is None
    assert any("HTTP 500" in record.message for record in caplog.records)
