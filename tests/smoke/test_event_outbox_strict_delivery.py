"""The outbox acknowledges delivery only after a successful strict HTTP send."""

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def gateway(monkeypatch):
    from helpers import dual_write

    monkeypatch.setattr(dual_write, "EVENT_GATEWAY_ENABLED", True)
    monkeypatch.setattr(
        dual_write, "EVENT_GATEWAY_URL", "https://gateway.example.invalid"
    )
    monkeypatch.setattr(dual_write, "EVENT_GATEWAY_HMAC_KEY", "fixture-signing-key")
    monkeypatch.setattr(dual_write, "EVENT_GATEWAY_HMAC_KEY_ID", "fixture-key")
    return dual_write


@pytest.fixture
def event():
    return {
        "source": "aist-bot",
        "external_id": "payment-fixture-1",
        "event_type": "payment_received",
        "schema_version": "v1",
        "occurred_at": datetime(2026, 9, 12, tzinfo=timezone.utc),
        "account_id": None,
        "payload": {"amount": 100, "currency": "RUB"},
    }


def fake_session(gateway, monkeypatch, *, status=200, failure=None):
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value="private-response")
    response.json = AsyncMock(
        return_value=(
            {"inserted": True, "id": "fixture-event-42"}
            if status == 201
            else {"inserted": False, "idempotent": True}
        )
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    session = MagicMock()
    session.response = response
    session.post.return_value = context
    if failure is not None:
        session.post.side_effect = failure
    getter = MagicMock(return_value=session)
    monkeypatch.setattr(gateway, "_get_session", getter)
    return getter, session


def test_disabled_strict_writer_refuses_without_http(gateway, event, monkeypatch):
    getter, session = fake_session(gateway, monkeypatch)
    monkeypatch.setattr(gateway, "EVENT_GATEWAY_ENABLED", False)

    with pytest.raises(RuntimeError, match="gateway is disabled"):
        asyncio.run(gateway.post_event_or_raise(**event))

    getter.assert_not_called()
    session.post.assert_not_called()


def test_disabled_best_effort_writer_remains_noop(gateway, event, monkeypatch):
    getter, session = fake_session(gateway, monkeypatch)
    monkeypatch.setattr(gateway, "EVENT_GATEWAY_ENABLED", False)

    asyncio.run(gateway.post_event(**event))

    getter.assert_not_called()
    session.post.assert_not_called()


@pytest.mark.parametrize(
    "status", [101, 199, 202, 204, 300, 302, 304, 307, 308, 400, 429, 500]
)
def test_strict_writer_refuses_undocumented_status_without_following_redirects(
    gateway,
    event,
    monkeypatch,
    status,
):
    _, session = fake_session(gateway, monkeypatch, status=status)

    with pytest.raises(RuntimeError, match=f"POST failed: HTTP {status}") as rejected:
        asyncio.run(gateway.post_event_or_raise(**event))

    assert "private-response" not in str(rejected.value)
    session.response.text.assert_not_awaited()
    assert session.post.call_args.kwargs["allow_redirects"] is False
    assert session.post.call_count == 1


@pytest.mark.parametrize("status", [200, 201])
def test_strict_writer_acknowledges_direct_success(gateway, event, monkeypatch, status):
    _, session = fake_session(gateway, monkeypatch, status=status)

    asyncio.run(gateway.post_event_or_raise(**event))

    assert session.post.call_count == 1
    assert session.post.call_args.args == ("https://gateway.example.invalid/events",)
    sent = session.post.call_args.kwargs
    assert sent["allow_redirects"] is False
    assert json.loads(sent["data"])["external_id"] == event["external_id"]
    headers = sent["headers"]
    assert headers["X-IWE-Signature-Version"] == "v1"
    assert headers["X-IWE-Key-Id"] == "fixture-key"
    canonical = (
        f"v1\naist-bot\nfixture-key\n{headers['X-IWE-Timestamp']}\n".encode()
        + sent["data"]
    )
    expected = hmac.new(b"fixture-signing-key", canonical, hashlib.sha256).hexdigest()
    assert headers["X-IWE-Signature"] == f"sha256={expected}"


@pytest.mark.parametrize(
    "status,acknowledgement",
    [
        (200, None),
        (200, []),
        (200, {}),
        (200, {"inserted": True, "id": "fixture-id"}),
        (200, {"inserted": 0, "idempotent": True}),
        (200, {"inserted": False, "idempotent": False}),
        (200, {"inserted": False, "idempotent": True, "error": "private-response"}),
        (201, {"inserted": False, "id": "fixture-id"}),
        (201, {"inserted": True}),
        (201, {"inserted": True, "id": None}),
        (201, {"inserted": True, "id": " "}),
    ],
)
def test_strict_writer_rejects_false_success_acknowledgement(
    gateway,
    event,
    monkeypatch,
    status,
    acknowledgement,
):
    _, session = fake_session(gateway, monkeypatch, status=status)
    session.response.json.return_value = acknowledgement

    with pytest.raises(RuntimeError, match="invalid acknowledgement") as rejected:
        asyncio.run(gateway.post_event_or_raise(**event))

    assert "private-response" not in str(rejected.value)
    session.response.json.assert_awaited_once()


def test_malformed_json_acknowledgement_does_not_expose_response(
    gateway,
    event,
    monkeypatch,
):
    _, session = fake_session(gateway, monkeypatch)
    session.response.json.side_effect = ValueError("private-response")

    with pytest.raises(RuntimeError, match="invalid acknowledgement") as rejected:
        asyncio.run(gateway.post_event_or_raise(**event))

    assert "private-response" not in str(rejected.value)
    assert rejected.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "failure", [ConnectionError("transport"), TimeoutError("timeout")]
)
def test_strict_writer_propagates_transport_failure(
    gateway, event, monkeypatch, failure
):
    _, session = fake_session(gateway, monkeypatch, failure=failure)

    with pytest.raises(type(failure), match=str(failure)):
        asyncio.run(gateway.post_event_or_raise(**event))

    assert session.post.call_count == 1


def install_outbox(event, monkeypatch):
    from core import scheduler
    from db.queries import event_outbox

    row = {
        "id": 17,
        "attempts": 0,
        "delivered_at": None,
        **{
            key: event[key]
            for key in (
                "external_id",
                "event_type",
                "occurred_at",
                "account_id",
                "payload",
            )
        },
    }
    pool = MagicMock()
    connection = pool.acquire.return_value.__aenter__.return_value
    connection.transaction = MagicMock()

    async def pending(_connection, batch):
        assert batch == 20
        return [row] if row["delivered_at"] is None else []

    async def delivered(_connection, row_id):
        assert row_id == row["id"]
        row["delivered_at"] = "fixture-delivered"

    async def failed(_connection, row_id, reason):
        assert row_id == row["id"]
        row["attempts"] += 1
        row["last_error"] = reason

    monkeypatch.setattr(scheduler, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(
        event_outbox, "fetch_pending_outbox", AsyncMock(side_effect=pending)
    )
    mark_delivered = AsyncMock(side_effect=delivered)
    mark_failed = AsyncMock(side_effect=failed)
    monkeypatch.setattr(event_outbox, "mark_delivered", mark_delivered)
    monkeypatch.setattr(event_outbox, "mark_failed", mark_failed)
    return scheduler, row, mark_delivered, mark_failed


def test_disabled_drain_retains_row_until_enabled_success(gateway, event, monkeypatch):
    _, session = fake_session(gateway, monkeypatch)
    scheduler, row, mark_delivered, mark_failed = install_outbox(event, monkeypatch)
    monkeypatch.setattr(gateway, "EVENT_GATEWAY_ENABLED", False)

    asyncio.run(scheduler._drain_event_outbox())

    session.post.assert_not_called()
    mark_delivered.assert_not_awaited()
    assert mark_failed.await_count == 1
    assert row["delivered_at"] is None
    assert row["attempts"] == 1
    assert "gateway is disabled" in row["last_error"]

    monkeypatch.setattr(gateway, "EVENT_GATEWAY_ENABLED", True)
    asyncio.run(scheduler._drain_event_outbox())

    assert session.post.call_count == 1
    assert mark_delivered.await_count == 1
    assert mark_failed.await_count == 1
    assert row["delivered_at"] == "fixture-delivered"
    assert (
        json.loads(session.post.call_args.kwargs["data"])["external_id"]
        == row["external_id"]
    )


@pytest.mark.parametrize("status", [202, 204, 302, 304, 429, 500])
def test_failed_http_drain_leaves_delivery_pending(gateway, event, monkeypatch, status):
    _, session = fake_session(gateway, monkeypatch, status=status)
    scheduler, row, mark_delivered, mark_failed = install_outbox(event, monkeypatch)

    asyncio.run(scheduler._drain_event_outbox())

    assert session.post.call_count == 1
    mark_delivered.assert_not_awaited()
    assert mark_failed.await_count == 1
    assert row["delivered_at"] is None
    assert row["attempts"] == 1
    assert "private-response" not in row["last_error"]
    session.response.text.assert_not_awaited()


def test_false_200_acknowledgement_keeps_outbox_pending(gateway, event, monkeypatch):
    _, session = fake_session(gateway, monkeypatch)
    session.response.json.return_value = {"error": "private-response"}
    scheduler, row, mark_delivered, mark_failed = install_outbox(event, monkeypatch)

    asyncio.run(scheduler._drain_event_outbox())

    mark_delivered.assert_not_awaited()
    assert mark_failed.await_count == 1
    assert row["delivered_at"] is None
    assert row["attempts"] == 1
    assert "private-response" not in row["last_error"]


def test_cancelled_drain_does_not_record_delivery_or_failure(
    gateway, event, monkeypatch
):
    _, session = fake_session(gateway, monkeypatch, failure=asyncio.CancelledError())
    scheduler, row, mark_delivered, mark_failed = install_outbox(event, monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler._drain_event_outbox())

    assert session.post.call_count == 1
    mark_delivered.assert_not_awaited()
    mark_failed.assert_not_awaited()
    assert row["delivered_at"] is None
    assert row["attempts"] == 0
