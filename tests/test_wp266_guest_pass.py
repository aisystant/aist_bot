"""WP-266 Ф6: unit checks for one-time Guest Pass persistence."""

import os
import sys
from datetime import datetime, timezone
from uuid import UUID

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import db.queries.guest_pass as guest_pass  # noqa: E402


ACCOUNT_ID = "2f95ffde-a9cf-4992-8cc3-f438a5284f05"
GRANTER_ID = UUID("3982069a-1790-482f-bec5-06b886c64821")
PASS_ID = UUID("f2e28255-0d03-4ae7-95b8-f73b73ca7ce0")
CONTRACT_ID = UUID("13f5fc2a-09ac-4562-967f-794485d2316d")


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


def patch_pool(monkeypatch, row):
    conn = FakeConn(row)

    async def get_pool():
        return FakePool(conn)

    monkeypatch.setattr(guest_pass, "get_subscription_pool", get_pool)
    return conn


def test_token_hash_is_stable_and_does_not_expose_token():
    digest = guest_pass._token_hash("one-time-secret")

    assert digest == "9769f061ca1f0907de3ca4da3f23937ea28a5cea0b048c0b5ee585f73fa92dbc"
    assert "one-time-secret" not in digest


@pytest.mark.asyncio
async def test_create_guest_pass_sends_only_hash_to_database(monkeypatch):
    expires_at = datetime(2026, 8, 18, tzinfo=timezone.utc)
    conn = patch_pool(
        monkeypatch,
        {"pass_id": PASS_ID, "claim_expires_at": expires_at},
    )
    monkeypatch.setattr(guest_pass.secrets, "token_urlsafe", lambda _: "raw-token")

    created = await guest_pass.create_guest_pass(ACCOUNT_ID)

    assert created.token == "raw-token"
    assert created.pass_id == PASS_ID
    assert conn.calls[0][1] == (UUID(ACCOUNT_ID), guest_pass._token_hash("raw-token"))
    assert "raw-token" not in conn.calls[0][1]


@pytest.mark.asyncio
async def test_activate_guest_pass_returns_access_contract(monkeypatch):
    valid_to = datetime(2026, 8, 18, tzinfo=timezone.utc)
    conn = patch_pool(
        monkeypatch,
        {
            "pass_id": PASS_ID,
            "granter_account_id": GRANTER_ID,
            "access_valid_to": valid_to,
            "contract_id": CONTRACT_ID,
        },
    )

    activation = await guest_pass.activate_guest_pass("raw-token", ACCOUNT_ID)

    assert activation.granter_account_id == GRANTER_ID
    assert activation.contract_id == CONTRACT_ID
    assert conn.calls[0][1] == (guest_pass._token_hash("raw-token"), UUID(ACCOUNT_ID))


@pytest.mark.asyncio
async def test_activate_guest_pass_rejects_invalid_token_before_database(monkeypatch):
    async def unexpected_pool():
        raise AssertionError("database must not be called")

    monkeypatch.setattr(guest_pass, "get_subscription_pool", unexpected_pool)

    with pytest.raises(guest_pass.GuestPassError, match="guest_pass_not_found"):
        await guest_pass.activate_guest_pass("x" * 49, ACCOUNT_ID)
