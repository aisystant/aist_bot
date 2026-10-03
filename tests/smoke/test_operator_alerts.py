"""Operator recovery remains available when normal delivery/storage is broken."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import core.notification_service as delivery
import core.operator_alerts as alerts
import db.queries.event_outbox as outbox
import handlers.subscription_stars as stars
from core import scheduler
from db import connection
from handlers import payments

pytestmark = pytest.mark.asyncio
DEVELOPER_ID = 90001
USER_ID = 12345
CHARGE_ID = "test-charge-567"


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(alerts, "DEVELOPER_CHAT_ID", DEVELOPER_ID)
    # No database or regular delivery is available for this entire test module.
    monkeypatch.setattr(
        connection, "get_pool", AsyncMock(side_effect=ConnectionError("db unavailable"))
    )
    monkeypatch.setattr(
        delivery,
        "enqueue",
        AsyncMock(side_effect=AssertionError("emergency used queue")),
    )
    return SimpleNamespace(
        send_message=AsyncMock(), session=SimpleNamespace(close=AsyncMock())
    )


def payment_message(bot, payload):
    return SimpleNamespace(
        bot=bot,
        chat=SimpleNamespace(id=USER_ID),
        successful_payment=SimpleNamespace(
            invoice_payload=payload,
            telegram_payment_charge_id=CHARGE_ID,
            total_amount=100,
            is_first_recurring=True,
            is_recurring=True,
        ),
        answer=AsyncMock(),
    )


@pytest.mark.parametrize(
    ("sender", "fields", "fragment"),
    [
        (
            alerts.alert_recurring_donation_failure,
            {"chat_id": USER_ID, "charge_id": CHARGE_ID},
            "Recurring donation НЕ сохранён",
        ),
        (
            alerts.alert_stars_subscription_failure,
            {
                "chat_id": USER_ID,
                "charge_id": CHARGE_ID,
                "stars_amount": 100,
                "tariff_key": "1m",
            },
            "Stars-подписка НЕ сохранена",
        ),
        (alerts.alert_event_outbox_stuck, {"stuck_count": 7}, "7 событий"),
    ],
)
async def test_operator_delivery_is_fixed_plain_bounded_and_database_independent(
    bot, sender, fields, fragment
):
    assert await sender(bot, **fields) is True

    sent = bot.send_message.await_args
    assert sent.args[0] == DEVELOPER_ID
    assert fragment in sent.args[1]
    assert sent.kwargs == {
        "parse_mode": None,
        "protect_content": True,
        "request_timeout": 5,
    }
    if "charge_id" in fields:
        assert CHARGE_ID in sent.args[1]
        assert str(USER_ID) in sent.args[1]
    connection.get_pool.assert_not_awaited()
    delivery.enqueue.assert_not_awaited()
    bot.session.close.assert_not_awaited()


async def test_missing_operator_recipient_never_sends_to_payment_user(bot, monkeypatch):
    monkeypatch.setattr(alerts, "DEVELOPER_CHAT_ID", 0)
    assert (
        await alerts.alert_recurring_donation_failure(
            bot, chat_id=USER_ID, charge_id=CHARGE_ID
        )
        is False
    )
    bot.send_message.assert_not_awaited()


async def test_transport_failure_does_not_log_exception_payload(bot, caplog):
    bot.send_message.side_effect = RuntimeError("private-connection-details")
    with caplog.at_level(logging.ERROR):
        assert await alerts.alert_event_outbox_stuck(bot, stuck_count=2) is False
    assert "RuntimeError" in caplog.text
    assert "private-connection-details" not in caplog.text
    bot.session.close.assert_not_awaited()


async def test_transport_timeout_is_bounded_even_if_client_ignores_timeout(
    bot, monkeypatch
):
    cancelled = asyncio.Event()

    async def stalled_send(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(alerts, "SEND_TIMEOUT_SECONDS", 0.01)
    bot.send_message.side_effect = stalled_send
    result = await asyncio.wait_for(
        alerts.alert_event_outbox_stuck(bot, stuck_count=2), timeout=1
    )
    assert result is False
    assert cancelled.is_set()


async def test_caller_cancellation_is_not_swallowed(bot):
    bot.send_message.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await alerts.alert_event_outbox_stuck(bot, stuck_count=2)


async def test_total_database_failure_still_alerts_and_answers_donation_user(
    bot, monkeypatch, caplog
):
    message = payment_message(bot, "sub_12345_100")
    monkeypatch.setattr(
        payments,
        "get_intern",
        AsyncMock(side_effect=ConnectionError("private-language")),
    )
    save = AsyncMock(side_effect=ConnectionError("private-storage"))
    monkeypatch.setattr(payments, "save_subscription", save)
    grant = AsyncMock()
    monkeypatch.setattr(payments, "upsert_subscription_grant", grant)

    with caplog.at_level(logging.WARNING):
        await payments.on_successful_payment(message)

    save.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    assert CHARGE_ID in bot.send_message.await_args.args[1]
    assert "private-" not in bot.send_message.await_args.args[1]
    assert "private-" not in caplog.text
    grant.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert "обработка занимает больше времени" in message.answer.await_args.args[0]


async def test_failed_operator_send_does_not_hide_donation_failure_from_user(
    bot, monkeypatch
):
    message = payment_message(bot, "sub_12345_100")
    monkeypatch.setattr(payments, "get_intern", AsyncMock(return_value=None))
    monkeypatch.setattr(
        payments, "save_subscription", AsyncMock(side_effect=ConnectionError("db down"))
    )
    bot.send_message.side_effect = RuntimeError("operator unavailable")

    await payments.on_successful_payment(message)

    bot.send_message.assert_awaited_once()
    message.answer.assert_awaited_once()
    assert "обработка занимает больше времени" in message.answer.await_args.args[0]


async def test_saved_donation_with_failed_confirmation_does_not_raise_storage_alert(
    bot, monkeypatch
):
    message = payment_message(bot, "sub_12345_100")
    message.answer.side_effect = RuntimeError("user unavailable")
    monkeypatch.setattr(
        payments, "get_intern", AsyncMock(return_value={"language": "ru"})
    )
    save = AsyncMock()
    monkeypatch.setattr(payments, "save_subscription", save)
    grant = AsyncMock()
    monkeypatch.setattr(payments, "upsert_subscription_grant", grant)

    await payments.on_successful_payment(message)

    save.assert_awaited_once()
    grant.assert_awaited_once()
    message.answer.assert_awaited_once()
    bot.send_message.assert_not_awaited()


async def test_stars_exhausted_retries_send_one_alert_with_recovery_details(
    bot, monkeypatch, caplog
):
    message = payment_message(bot, "stars_sub_12345_1m")
    monkeypatch.setattr(stars, "resolve_ory_id_from_chat", AsyncMock(return_value=None))
    save = AsyncMock(side_effect=ConnectionError("private-stars-storage"))
    monkeypatch.setattr(stars, "save_subscription_with_outbox", save)
    sleep = AsyncMock()
    monkeypatch.setattr(stars.asyncio, "sleep", sleep)

    with caplog.at_level(logging.WARNING):
        await stars.on_successful_stars_sub(message)

    assert save.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4]
    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.args[1]
    assert CHARGE_ID in text
    assert "100 XTR" in text
    assert "tariff=1m" in text
    assert "private-stars-storage" not in text
    assert "private-stars-storage" not in caplog.text
    message.answer.assert_awaited_once()
    assert "обрабатывается дольше обычного" in message.answer.await_args.args[0]


async def test_stars_retry_success_does_not_send_false_storage_alert(bot, monkeypatch):
    message = payment_message(bot, "stars_sub_12345_1m")
    monkeypatch.setattr(stars, "resolve_ory_id_from_chat", AsyncMock(return_value=None))
    save = AsyncMock(side_effect=[ConnectionError("temporary"), None])
    monkeypatch.setattr(stars, "save_subscription_with_outbox", save)
    monkeypatch.setattr(stars.asyncio, "sleep", AsyncMock())

    await stars.on_successful_stars_sub(message)

    assert save.await_count == 2
    bot.send_message.assert_not_awaited()
    assert "Подписка активирована" in message.answer.await_args.args[0]
    assert save.await_args_list[0].kwargs == save.await_args_list[1].kwargs


async def test_stars_saved_before_failed_confirmation_does_not_raise_storage_alert(
    bot, monkeypatch
):
    message = payment_message(bot, "stars_sub_12345_1m")
    message.answer.side_effect = RuntimeError("user unavailable")
    monkeypatch.setattr(stars, "resolve_ory_id_from_chat", AsyncMock(return_value=None))
    save = AsyncMock()
    monkeypatch.setattr(stars, "save_subscription_with_outbox", save)

    with pytest.raises(RuntimeError, match="user unavailable"):
        await stars.on_successful_stars_sub(message)

    save.assert_awaited_once()
    bot.send_message.assert_not_awaited()


async def test_outbox_watch_retries_failed_alert_and_cools_down_after_ack(
    bot, monkeypatch
):
    monkeypatch.setattr(scheduler, "_bot_token", "000000000:AAFakeTokenForTests")
    monkeypatch.setattr(scheduler, "DEVELOPER_CHAT_ID", DEVELOPER_ID)
    monkeypatch.setattr(scheduler, "_last_outbox_watch_alert_ts", 0)
    monkeypatch.setattr(outbox, "count_stuck_outbox", AsyncMock(return_value=7))
    create_bot = Mock(return_value=bot)
    monkeypatch.setattr(scheduler, "Bot", create_bot)
    bot.send_message.side_effect = [RuntimeError("down"), None]

    await scheduler._watch_event_outbox()
    assert scheduler._last_outbox_watch_alert_ts == 0
    bot.session.close.assert_awaited_once()

    await scheduler._watch_event_outbox()
    assert scheduler._last_outbox_watch_alert_ts > 0
    assert bot.send_message.await_count == 2
    assert bot.session.close.await_count == 2

    await scheduler._watch_event_outbox()
    assert create_bot.call_count == 2
    assert bot.send_message.await_count == 2


async def test_empty_outbox_never_constructs_alert_bot(bot, monkeypatch):
    monkeypatch.setattr(outbox, "count_stuck_outbox", AsyncMock(return_value=0))
    create_bot = Mock(side_effect=AssertionError("unexpected alert"))
    monkeypatch.setattr(scheduler, "Bot", create_bot)

    await scheduler._watch_event_outbox()

    create_bot.assert_not_called()
    bot.send_message.assert_not_awaited()
