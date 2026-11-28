from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from core import marathon_digest_delivery as digest

ACCOUNT = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SENDER = "dddddddd-1111-4111-8111-dddddddddddd"
WEEK = "2026-W47"
KEY = f"marathon_digest:42:{WEEK}"


@pytest.fixture
def delivery(monkeypatch):
    center = SimpleNamespace(sender_id=SENDER, route=AsyncMock(return_value="center"))
    journal = SimpleNamespace(
        legacy=AsyncMock(return_value=True),
        read=AsyncMock(return_value=None),
        insert=AsyncMock(return_value=True),
        account=AsyncMock(return_value=ACCOUNT),
    )
    monkeypatch.setattr(digest, "try_insert_notification", journal.legacy)
    monkeypatch.setattr(digest, "get_marathon_digest_route", journal.read)
    monkeypatch.setattr(digest, "insert_domain_event_direct", journal.insert)
    monkeypatch.setattr(digest, "_linked_account", journal.account)
    return center, journal


async def test_no_account_and_disabled_switch_keep_original_key(delivery):
    center, journal = delivery
    journal.account.return_value = None
    assert await digest.plan_digest(center, 42, WEEK) == ("legacy", None)
    assert await digest.plan_digest(None, 42, WEEK) == ("legacy", None)
    center.route.assert_not_awaited()
    assert journal.legacy.await_count == 2
    journal.legacy.assert_awaited_with(42, "marathon_digest", KEY)
    assert await digest.plan_digest(None, 42, WEEK, retry=True) is None


async def test_center_wins_only_after_claiming_the_same_key_as_legacy(delivery):
    center, journal = delivery
    assert await digest.plan_digest(center, 42, WEEK) == ("center", ACCOUNT)
    assert journal.insert.await_args.args[1] == f"notification-{KEY}"
    assert journal.insert.await_args.args[-1] == {"route": "center", "sender_id": SENDER}
    journal.insert.return_value = False
    assert await digest.plan_digest(center, 42, WEEK) is None
    journal.legacy.assert_not_awaited()


async def test_old_claim_blocks_center_even_after_subscription(delivery):
    center, journal = delivery
    journal.read.return_value = {"route": None, "finished": False}
    assert await digest.plan_digest(center, 42, WEEK) is None
    center.route.assert_not_awaited()
    journal.insert.assert_not_awaited()


async def test_restart_keeps_the_pinned_route_and_new_account_is_never_used(delivery):
    center, journal = delivery
    journal.read.return_value = {"route": "center", "account_id": UUID(ACCOUNT), "sender_id": SENDER, "finished": False}
    assert await digest.plan_digest(center, 42, WEEK, retry=True) == ("center", ACCOUNT)
    center.route.assert_not_awaited()
    journal.account.return_value = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
    with pytest.raises(digest.DigestDeliveryError, match="account link"):
        await digest.plan_digest(center, 42, WEEK, retry=True)
    journal.account.return_value = ACCOUNT
    center.sender_id = "eeeeeeee-1111-4111-8111-eeeeeeeeeeee"
    with pytest.raises(digest.DigestDeliveryError, match="sender has changed"):
        await digest.plan_digest(center, 42, WEEK, retry=True)
    journal.legacy.assert_not_awaited()


async def test_explicit_opt_out_pins_suppression(delivery):
    center, journal = delivery
    center.route.return_value = "suppress"
    assert await digest.plan_digest(center, 42, WEEK) is None
    assert journal.insert.await_args.args[-1] == {"route": "suppress", "sender_id": SENDER}
    journal.legacy.assert_not_awaited()


async def test_unavailable_routing_does_not_send_direct(delivery):
    center, journal = delivery
    center.route.side_effect = digest.DigestDeliveryError("unavailable")
    with pytest.raises(digest.DigestDeliveryError):
        await digest.plan_digest(center, 42, WEEK)
    journal.legacy.assert_not_awaited()
    journal.insert.assert_not_awaited()


async def test_week_key_survives_a_timeout_and_makes_the_first_accepted_release_authoritative(monkeypatch):
    center = digest.DigestCenter("https://center.test", "nck_synthetic000000000", bytes(32), SENDER)
    send = AsyncMock(side_effect=[digest.DigestDeliveryError("timeout"), (409, {"error": "release_key_conflict"})])
    monkeypatch.setattr(digest.DigestCenter, "_post", send)
    with pytest.raises(digest.DigestDeliveryError):
        await center.submit(WEEK, ACCOUNT, "Title", "First version")
    await center.submit(WEEK, ACCOUNT, "Title", "Later version")
    assert [c.args[1]["release_key"] for c in send.await_args_list] == [f"marathon-weekly:{WEEK}"] * 2
    assert all("chat_id" not in c.args[1] for c in send.await_args_list)
