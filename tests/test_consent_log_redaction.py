"""Regression tests for PII-safe consent logs."""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.connection as db_connection
from db.queries import consent as consent_queries


class _ConsentConn:
    def __init__(self):
        self.execute_results = iter(("UPDATE 2", "DELETE 1", "UPDATE 1"))

    async def fetchrow(self, *_args):
        return {
            "account_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "opt_in": True,
            "scope": ["stage_evaluation", "club_activity"],
            "opted_at": datetime.now(timezone.utc),
        }

    async def execute(self, *_args):
        return next(self.execute_results)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc_info):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def test_consent_write_logs_exclude_account_id_but_keep_operation_details(monkeypatch, caplog):
    """All three consent writers log useful non-identifying operational details."""
    subject = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    consent_pool = _Pool(_ConsentConn())
    learning_pool = _Pool(_ConsentConn())

    async def get_consent_pool():
        return consent_pool

    async def get_learning_pool():
        return learning_pool

    async def no_op_post_event(**_kwargs):
        return None

    monkeypatch.setattr(consent_queries, "get_consent_pool", get_consent_pool)
    monkeypatch.setattr(db_connection, "get_learning_pool", get_learning_pool)
    monkeypatch.setattr("helpers.dual_write.post_event", no_op_post_event)
    caplog.set_level(logging.DEBUG, logger=consent_queries.__name__)

    asyncio.run(consent_queries.set_consent(subject, opt_in=True))
    asyncio.run(consent_queries.revoke_consent(subject))
    asyncio.run(consent_queries.set_consent_grant(subject, "typing_tracking", granted=True))
    asyncio.run(asyncio.sleep(0))

    assert subject not in caplog.text
    assert "opt_in=True" in caplog.text
    assert "scope_count=2" in caplog.text
    assert "consent_grant_rows=2" in caplog.text
    assert "tracking_consent_rows=1" in caplog.text
    assert "scope=typing_tracking" in caplog.text
    assert "granted=True" in caplog.text
