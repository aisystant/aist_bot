"""Actual payment handlers must receive their own product in startup order."""

import ast
import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Router
from aiogram.types import Message


@pytest.fixture
def payment_pipeline(monkeypatch):
    import handlers
    from handlers import payments, showcase, subscription_stars, workshop
    from helpers import dual_write

    modules = (workshop, showcase, subscription_stars, payments)
    for module in modules:
        monkeypatch.setattr(
            module, "get_intern", AsyncMock(return_value={"language": "ru"})
        )

    writes = {
        "workshop": AsyncMock(return_value=1),
        "showcase": AsyncMock(return_value=1),
        "subscription": AsyncMock(),
        "donation": AsyncMock(),
    }
    monkeypatch.setattr(workshop, "create_and_confirm_payment", writes["workshop"])
    monkeypatch.setattr(workshop, "_notify_aisystant_stars_payment", AsyncMock())
    monkeypatch.setattr(workshop, "_emit_stars_payment_received", AsyncMock())
    monkeypatch.setattr(workshop, "_send_direct_masterskaya_invite", AsyncMock())
    monkeypatch.setattr(showcase, "create_seminar_payment", writes["showcase"])
    monkeypatch.setattr(
        showcase, "get_seminar_by_code", AsyncMock(return_value={"code": "demo"})
    )
    monkeypatch.setattr(showcase, "_send_seminar_access", AsyncMock())
    monkeypatch.setattr(dual_write, "emit_payment_received", AsyncMock())
    monkeypatch.setattr(
        subscription_stars, "resolve_ory_id_from_chat", AsyncMock(return_value=None)
    )
    # Exercise the deployed persister and pilot's outbox variant with the same
    # routing assertion, without importing the unrelated subscription changes.
    for name in ("save_subscription", "save_subscription_with_outbox"):
        if hasattr(subscription_stars, name):
            monkeypatch.setattr(subscription_stars, name, writes["subscription"])
    if hasattr(subscription_stars, "post_event"):
        monkeypatch.setattr(subscription_stars, "post_event", AsyncMock())
    monkeypatch.setattr(payments, "save_subscription", writes["donation"])
    monkeypatch.setattr(payments, "upsert_subscription_grant", AsyncMock())

    registrations = {
        "workshop_router": workshop.workshop_router,
        "showcase_router": showcase.showcase_router,
        "subscription_stars_router": subscription_stars.subscription_stars_router,
        "payments_router": payments.payments_router,
    }
    # Read startup order without attaching or replacing the application singletons.
    setup = ast.parse(inspect.getsource(handlers.setup_handlers)).body[0]
    pipeline = Router()
    attached = []
    for statement in setup.body:
        if not isinstance(statement, ast.Expr) or not isinstance(
            statement.value, ast.Call
        ):
            continue
        call = statement.value
        if (
            not isinstance(call.func, ast.Attribute)
            or call.func.attr != "include_router"
        ):
            continue
        if not call.args or not isinstance(call.args[0], ast.Name):
            continue
        name = call.args[0].id
        if name not in registrations:
            continue
        router = Router(name=name)
        router.message.handlers.extend(registrations[name].message.handlers)
        pipeline.include_router(router)
        attached.append(name)
    assert set(attached) == set(registrations)
    return SimpleNamespace(router=pipeline, writes=writes, modules=modules)


def _payment_message(bot, payload, amount):
    return Message.model_validate(
        {
            "message_id": 1,
            "date": datetime.now(timezone.utc),
            "chat": {"id": 101, "type": "private"},
            "from": {"id": 101, "is_bot": False, "first_name": "Test"},
            "successful_payment": {
                "currency": "XTR",
                "total_amount": amount,
                "invoice_payload": payload,
                "telegram_payment_charge_id": "routing-test-charge",
                "provider_payment_charge_id": "",
            },
        }
    ).as_(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,amount,writer",
    [
        ("workshop_direct_101", 4000, "workshop"),
        ("seminar_demo_101", 500, "showcase"),
        ("stars_sub_101_1m", 500, "subscription"),
        ("sub_101_100", 100, "donation"),
    ],
)
async def test_successful_payment_reaches_its_actual_product_handler(
    payment_pipeline,
    bot,
    payload,
    amount,
    writer,
):
    event = _payment_message(bot, payload, amount)

    await payment_pipeline.router.propagate_event("message", event, bot=bot)

    for name, write in payment_pipeline.writes.items():
        if name == writer:
            write.assert_awaited_once()
            charge_key = (
                "payment_id" if name in {"workshop", "showcase"} else "charge_id"
            )
            assert write.await_args.kwargs[charge_key] == "routing-test-charge"
        else:
            write.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_time_donation_reaches_actual_thank_you_handler(
    payment_pipeline, bot, monkeypatch
):
    payments = payment_pipeline.modules[-1]
    create_invoice = AsyncMock(return_value="https://t.me/$test")
    monkeypatch.setattr(bot, "create_invoice_link", create_invoice)
    event = _payment_message(bot, "donate_once_101_100", 100)

    await payment_pipeline.router.propagate_event("message", event, bot=bot)

    assert payments.t("donation.once_success", "ru") in [
        message["text"] for message in bot.get_sent()
    ]
    create_invoice.assert_awaited_once()
    assert create_invoice.await_args.kwargs["payload"] == "sub_101_100"
    for write in payment_pipeline.writes.values():
        write.assert_not_awaited()
