"""
WP-5 F70 (2026-11-09): tool discovery on a cold Gateway.

Pilot bot logs 29.10, 02.11, 04.11: right after container start, tools/list
failed ~3 s after the request with an empty error text and the bot ran with
zero discovered tools until the first user question triggered a refresh.
Discovery now has its own timeout, logs the exception type and re-tries in
the background while the cache stays empty.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.gateway_mcp import GatewayMCPClient


class _PostCtx:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _tools_response(names):
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"result": {"tools": [
        {"name": n, "description": "d", "inputSchema": {"type": "object"}} for n in names
    ]}})
    return resp


@pytest.mark.asyncio
async def test_list_tools_uses_discovery_timeout_not_per_call_budget():
    client = GatewayMCPClient(url="http://gateway.test")
    session = MagicMock()
    session.post = MagicMock(return_value=_PostCtx(_tools_response(["a"])))

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)):
        tools = await client.list_tools()

    assert [t["name"] for t in tools] == ["a"]
    timeout = session.post.call_args.kwargs["timeout"]
    assert timeout.total == client.DISCOVERY_TIMEOUT
    assert client.DISCOVERY_TIMEOUT > client.DEFAULT_TIMEOUT


@pytest.mark.asyncio
async def test_list_tools_timeout_logs_exception_type(caplog):
    client = GatewayMCPClient(url="http://gateway.test")
    session = MagicMock()
    session.post = MagicMock(return_value=_PostCtx(exc=TimeoutError()))

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)), \
         caplog.at_level(logging.WARNING):
        tools = await client.list_tools()

    assert tools == []
    assert "list_tools failed: TimeoutError" in caplog.text


@pytest.mark.asyncio
async def test_bootstrap_retries_until_cache_filled_after_failed_first_load():
    client = GatewayMCPClient(url="http://gateway.test")
    client.DISCOVERY_RETRY_DELAYS = (0, 0, 0)
    session = MagicMock()
    session.post = MagicMock(side_effect=[
        _PostCtx(exc=TimeoutError()),
        _PostCtx(_tools_response(["a", "b"])),
    ])

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)):
        count = await client.bootstrap_tools_discovery()

    assert count == 2
    assert session.post.call_count == 2  # stops once the cache is filled
    assert [t["name"] for t in client.get_discovered_tools()] == ["a", "b"]


@pytest.mark.asyncio
async def test_bootstrap_does_not_retry_when_first_load_succeeds():
    client = GatewayMCPClient(url="http://gateway.test")
    client.DISCOVERY_RETRY_DELAYS = (0, 0, 0)
    session = MagicMock()
    session.post = MagicMock(return_value=_PostCtx(_tools_response(["a"])))

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)):
        count = await client.bootstrap_tools_discovery()

    assert count == 1
    assert session.post.call_count == 1


@pytest.mark.asyncio
async def test_concurrent_list_tools_sends_single_request():
    """While one discovery is in flight, other callers get the current cache
    instead of firing their own request (review finding: fan-out of refreshes)."""
    client = GatewayMCPClient(url="http://gateway.test")
    release = asyncio.Event()
    resp = _tools_response(["a"])

    class _SlowCtx(_PostCtx):
        async def __aenter__(self):
            await release.wait()
            return resp

    session = MagicMock()
    session.post = MagicMock(return_value=_SlowCtx())

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)):
        first = asyncio.create_task(client.list_tools())
        await asyncio.sleep(0)
        second = await client.list_tools()
        release.set()
        first_result = await first

    assert second == []
    assert [t["name"] for t in first_result] == ["a"]
    assert session.post.call_count == 1


@pytest.mark.asyncio
async def test_discovery_does_not_take_user_call_semaphore_slot():
    client = GatewayMCPClient(url="http://gateway.test")
    release = asyncio.Event()
    resp = _tools_response(["a"])
    seen_free_slots = []

    class _ProbeCtx(_PostCtx):
        async def __aenter__(self):
            seen_free_slots.append(client._call_semaphore._value)
            await release.wait()
            return resp

    session = MagicMock()
    session.post = MagicMock(return_value=_ProbeCtx())

    with patch.object(client, "_get_session", new=AsyncMock(return_value=session)):
        task = asyncio.create_task(client.list_tools())
        await asyncio.sleep(0)
        release.set()
        await task

    assert seen_free_slots == [12]
