"""Регрессии корректности и наблюдаемости onboarding (WP-330 Ф16)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _AcquireContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_update_intern_keeps_mapping_for_dual_write(monkeypatch):
    from clients.gateway_mcp import gateway_mcp
    from db.queries import users

    connection = AsyncMock()
    connection.transaction = MagicMock(return_value=_AcquireContext(None))
    pool = MagicMock()
    pool.acquire.return_value = _AcquireContext(connection)
    monkeypatch.setattr(users, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(
        users,
        "resolve_ory_id_from_chat",
        AsyncMock(return_value="11111111-2222-3333-4444-555555555555"),
    )
    post_event = AsyncMock()
    monkeypatch.setattr(users, "post_event", post_event)

    def close_task(coroutine):
        coroutine.close()
        return MagicMock()

    monkeypatch.setattr(users.asyncio, "create_task", close_task)

    with patch.object(gateway_mcp, "is_connected", return_value=False):
        await users.update_intern(
            123,
            name="Test",
            onboarding_completed=True,
        )

    assert connection.execute.await_count == 2
    connection.transaction.assert_called_once_with()
    payload = post_event.call_args.kwargs["payload"]
    assert payload["fields_updated"] == ["name", "onboarding_completed"]
    assert payload["fields_count"] == 2


@pytest.mark.asyncio
async def test_cohort_lookup_uses_ory_uuid_not_aisystant_id(monkeypatch):
    from db.queries import onboarding_journey
    from helpers import dual_write

    account_id = "11111111-2222-3333-4444-555555555555"
    resolve = AsyncMock(return_value=account_id)
    get_state = AsyncMock(return_value={"cohort_id": "R2"})
    monkeypatch.setattr(dual_write, "resolve_ory_id_from_chat", resolve)
    monkeypatch.setattr(onboarding_journey, "get_onboarding_state", get_state)

    cohort = await onboarding_journey.get_cohort_id_for_chat(123)

    assert cohort == "R2"
    resolve.assert_awaited_once_with(123)
    get_state.assert_awaited_once_with(account_id)


@pytest.mark.asyncio
async def test_traced_acquire_records_pool_wait():
    from core import tracing

    connection = object()
    pool = MagicMock()
    pool.acquire.return_value = _AcquireContext(connection)
    trace = tracing.start_trace(123, "/start", "onboarding")

    async with tracing.traced_acquire(pool, "db.test") as acquired:
        assert acquired is connection

    assert [item.name for item in trace.spans] == ["db.test.pool_acquire"]
    tracing._current_trace.set(None)


@pytest.mark.asyncio
async def test_nav_alert_does_not_claim_database_cause(monkeypatch):
    from db.queries import traces

    connection = AsyncMock()
    connection.fetch.return_value = [
        {"command": "/start", "total_ms": 5001, "state": None},
        {"command": "cb:onboarder_start", "total_ms": 4500, "state": None},
        {"command": "cb:link_check", "total_ms": 3500, "state": None},
    ]
    pool = MagicMock()
    pool.acquire.return_value = _AcquireContext(connection)
    monkeypatch.setattr(traces, "get_learning_pool", AsyncMock(return_value=pool))

    alert = await traces.check_nav_latency_alerts()

    assert "Алерт: медленная навигация" in alert
    assert "Причина не установлена" in alert
    assert "нагрузка на пул" not in alert
    assert "DB pool exhaustion" not in alert
