"""Regression coverage for direct Workshop purchases (WP-572)."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import F, Router
from aiogram.types import Message


@pytest.fixture
def workshop(monkeypatch):
    from handlers import workshop as module

    for name, value in {
        "get_intern": {"language": "ru"},
        "has_direct_masterskaya_payment": False,
        "get_workshop_payment_count": 0,
        "create_and_confirm_payment": 1,
        "confirm_burn": True,
        "_notify_aisystant_stars_payment": None,
        "_emit_stars_payment_received": None,
        "_send_direct_masterskaya_invite": None,
        "_send_invite_by_count": None,
    }.items():
        monkeypatch.setattr(module, name, AsyncMock(return_value=value))
    return module


@pytest.fixture
def message():
    return SimpleNamespace(
        chat=SimpleNamespace(id=101),
        bot=SimpleNamespace(
            create_invoice_link=AsyncMock(return_value="https://t.me/$test")
        ),
        answer=AsyncMock(),
        successful_payment=SimpleNamespace(
            invoice_payload="workshop_direct_101",
            currency="XTR",
            total_amount=4000,
            telegram_payment_charge_id="test-charge",
        ),
    )


@pytest.fixture
def callback(message):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=101),
        message=message,
        bot=message.bot,
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_paid_direct_entry_recovers_workshop_invite(workshop, message):
    workshop.has_direct_masterskaya_payment.return_value = True
    await workshop.show_direct_masterskaya_card(message)
    workshop._send_direct_masterskaya_invite.assert_awaited_once_with(
        message.bot,
        101,
        "ru",
        message,
    )
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_direct_payment_does_not_send_seminar_invite(workshop, callback):
    workshop.has_direct_masterskaya_payment.return_value = True
    workshop.get_workshop_payment_count.return_value = 1
    await workshop.callback_seminar_check(callback)
    workshop._send_direct_masterskaya_invite.assert_awaited_once()
    workshop._send_invite_by_count.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_first_seminar_payment_still_sends_seminar(workshop, callback):
    workshop.get_workshop_payment_count.return_value = 1
    await workshop.callback_seminar_check(callback)
    workshop._send_invite_by_count.assert_awaited_once_with(
        callback.bot,
        101,
        1,
        "ru",
        callback.message,
    )
    workshop._send_direct_masterskaya_invite.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        "callback_direct_masterskaya_pay_stars",
        "callback_direct_masterskaya_pay_rub",
    ],
)
async def test_old_pay_button_recovers_access_without_another_charge(
    workshop, callback, handler, monkeypatch
):
    workshop.has_direct_masterskaya_payment.return_value = True
    provider = SimpleNamespace(create_payment=AsyncMock())
    monkeypatch.setattr(workshop, "_get_yookassa", lambda: provider)
    await getattr(workshop, handler)(callback)
    workshop._send_direct_masterskaya_invite.assert_awaited_once()
    callback.bot.create_invoice_link.assert_not_awaited()
    provider.create_payment.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_stars_invoice_has_exact_price_and_buyer(workshop, callback):
    await workshop.callback_direct_masterskaya_pay_stars(callback)
    params = callback.bot.create_invoice_link.await_args.kwargs
    assert params["currency"] == "XTR"
    assert params["payload"] == "workshop_direct_101"
    assert len(params["prices"]) == 1
    assert params["prices"][0].amount == 4000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "currency,amount,payload,accepted",
    [
        ("XTR", 4000, "workshop_direct_101", True),
        ("XTR", 1, "workshop_direct_101", False),
        ("RUB", 4000, "workshop_direct_101", False),
        ("XTR", 4000, "workshop_direct_202", False),
        ("XTR", 4000, "workshop_direct_bad", False),
    ],
)
async def test_direct_pre_checkout_validates_invoice(
    workshop, currency, amount, payload, accepted
):
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=101),
        currency=currency,
        total_amount=amount,
        invoice_payload=payload,
        answer=AsyncMock(),
    )
    await workshop.on_workshop_pre_checkout(query)
    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs["ok"] is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", [True, RuntimeError("database unavailable")])
async def test_direct_pre_checkout_rejects_existing_access_or_lookup_failure(
    workshop, lookup
):
    if isinstance(lookup, Exception):
        workshop.has_direct_masterskaya_payment.side_effect = lookup
    else:
        workshop.has_direct_masterskaya_payment.return_value = lookup
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=101),
        currency="XTR",
        total_amount=4000,
        invoice_payload="workshop_direct_101",
        answer=AsyncMock(),
    )
    await workshop.on_workshop_pre_checkout(query)
    assert query.answer.await_args.kwargs["ok"] is False


@pytest.mark.asyncio
async def test_successful_direct_payment_records_product_and_sends_invite(
    workshop, message
):
    await workshop.on_workshop_payment(message)
    workshop.create_and_confirm_payment.assert_awaited_once_with(
        telegram_id=101,
        amount=4000,
        source="stars",
        payment_id="test-charge",
        product="masterskaya_direct",
    )
    workshop._send_direct_masterskaya_invite.assert_awaited_once()
    workshop._emit_stars_payment_received.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_charge_does_not_repeat_side_effects(workshop, message):
    workshop.create_and_confirm_payment.side_effect = [1, 0]
    await workshop.on_workshop_payment(message)
    await workshop.on_workshop_payment(message)
    assert workshop.create_and_confirm_payment.await_count == 2
    workshop._send_direct_masterskaya_invite.assert_awaited_once()
    workshop._notify_aisystant_stars_payment.assert_awaited_once()
    workshop._emit_stars_payment_received.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_seminar_retries_discount_confirmation(workshop, message):
    message.successful_payment.invoice_payload = "workshop_seminar_101_p_discount"
    workshop.create_and_confirm_payment.side_effect = [1, 0]
    workshop.confirm_burn.side_effect = [RuntimeError("temporary failure"), True]
    workshop.get_workshop_payment_count.return_value = 1
    await workshop.on_workshop_payment(message)
    await workshop.on_workshop_payment(message)
    assert workshop.confirm_burn.await_count == 2
    workshop.confirm_burn.assert_awaited_with("discount")
    workshop._emit_stars_payment_received.assert_awaited_once()
    workshop._send_invite_by_count.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_invite_can_be_recovered_without_another_payment(
    workshop, message
):
    workshop._send_direct_masterskaya_invite.side_effect = [
        RuntimeError("delivery failed"),
        None,
    ]
    with pytest.raises(RuntimeError, match="delivery failed"):
        await workshop.on_workshop_payment(message)
    workshop.has_direct_masterskaya_payment.return_value = True
    await workshop.show_direct_masterskaya_card(message)
    assert workshop._send_direct_masterskaya_invite.await_count == 2
    workshop.create_and_confirm_payment.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_product_reaches_next_router(workshop):
    # Copy the actual registrations without reparenting the application's singleton.
    first = Router()
    first.message.handlers.extend(workshop.workshop_router.message.handlers)
    second = Router()
    downstream = AsyncMock()

    async def handle_other_payment(message):
        await downstream(message)

    second.message.register(handle_other_payment, F.successful_payment)
    parent = Router()
    parent.include_routers(first, second)
    event = Message.model_validate(
        {
            "message_id": 1,
            "date": datetime.now(timezone.utc),
            "chat": {"id": 101, "type": "private"},
            "from": {"id": 101, "is_bot": False, "first_name": "Test"},
            "successful_payment": {
                "currency": "XTR",
                "total_amount": 100,
                "invoice_payload": "stars_sub_test",
                "telegram_payment_charge_id": "other-charge",
                "provider_payment_charge_id": "",
            },
        }
    )
    await parent.propagate_event(
        update_type="message", event=event, bot=SimpleNamespace()
    )
    downstream.assert_awaited_once()
    workshop.create_and_confirm_payment.assert_not_awaited()
