"""Regression tests for PII-safe identifiers of consent audit events."""

import asyncio
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.connection as db_connection
from db.queries import consent as consent_queries


class _Connection:
    async def execute(self, *_args):
        return "INSERT 0 1"


class _Acquire:
    async def __aenter__(self):
        return _Connection()

    async def __aexit__(self, *_exc_info):
        return False


class _Pool:
    def acquire(self):
        return _Acquire()


def test_generate_consent_event_id_is_random_uuid4_without_subject_data():
    """The identifier is random and cannot include the account id used by the writer."""
    account_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    first_id = consent_queries.generate_consent_event_id()
    second_id = consent_queries.generate_consent_event_id()

    assert UUID(first_id).version == 4
    assert UUID(second_id).version == 4
    assert first_id != second_id
    assert account_id not in first_id
    assert account_id not in second_id


def test_set_consent_grant_posts_random_uuid4_external_id(monkeypatch):
    """The audit event receives the PII-safe identifier, not an account-derived string."""
    account_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    captured_events = []

    async def get_learning_pool():
        return _Pool()

    async def capture_post_event(**kwargs):
        captured_events.append(kwargs)

    async def exercise():
        await consent_queries.set_consent_grant(
            account_id,
            "typing_tracking",
            granted=True,
        )
        await asyncio.sleep(0)

    monkeypatch.setattr(db_connection, "get_learning_pool", get_learning_pool)
    monkeypatch.setattr("helpers.dual_write.post_event", capture_post_event)

    asyncio.run(exercise())

    assert len(captured_events) == 1
    external_id = captured_events[0]["external_id"]
    assert UUID(external_id).version == 4
    assert account_id not in external_id
