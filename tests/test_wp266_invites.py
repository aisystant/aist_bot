"""WP-266: persistent invite records attribution without granting access."""

import os
import sys
from uuid import UUID

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import db.queries.invites as invites  # noqa: E402

ACCOUNT_ID = "2f95ffde-a9cf-4992-8cc3-f438a5284f05"
GRANTER_ID = UUID("3982069a-1790-482f-bec5-06b886c64821")
LINK_ID = UUID("f2e28255-0d03-4ae7-95b8-f73b73ca7ce0")
ATTRIBUTION_ID = UUID("13f5fc2a-09ac-4562-967f-794485d2316d")


class FakeConn:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.row


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Acquisition:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False

        return Acquisition()


def patch_subscription_pool(monkeypatch, row):
    conn = FakeConn(row)

    async def get_pool():
        return FakePool(conn)

    monkeypatch.setattr(invites, "get_subscription_pool", get_pool)
    return conn


@pytest.mark.asyncio
async def test_get_or_create_invite_returns_persistent_code(monkeypatch):
    conn = patch_subscription_pool(
        monkeypatch,
        {"link_id": LINK_ID, "invite_code": "stable-public-code"},
    )

    link = await invites.get_or_create_invite(ACCOUNT_ID)

    assert link.code == "stable-public-code"
    assert conn.calls[0][1] == (UUID(ACCOUNT_ID),)
    assert "get_or_create_referral_link" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_activate_invite_records_attribution_without_contract(monkeypatch):
    conn = patch_subscription_pool(
        monkeypatch,
        {"attribution_id": ATTRIBUTION_ID, "granter_account_id": GRANTER_ID},
    )

    attribution = await invites.activate_invite("stable-public-code", ACCOUNT_ID)

    assert attribution.granter_account_id == GRANTER_ID
    sql, args = conn.calls[0]
    assert "activate_referral_link" in sql
    assert "contract" not in sql.lower()
    assert args == ("stable-public-code", UUID(ACCOUNT_ID))


@pytest.mark.asyncio
async def test_activate_invite_rejects_invalid_code_before_database(monkeypatch):
    async def unexpected_pool():
        raise AssertionError("database must not be called")

    monkeypatch.setattr(invites, "get_subscription_pool", unexpected_pool)

    with pytest.raises(invites.InviteError, match="invite_not_found"):
        await invites.activate_invite("x" * 65, ACCOUNT_ID)
