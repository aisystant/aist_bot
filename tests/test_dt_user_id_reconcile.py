"""peer-session 2026-06-25-09 — durable dt_user_id sync.

public.users keeps the Ory UUID in both `ory_id` and `dt_user_id`; some connect
paths set only `ory_id`, breaking /points (reads dt_user_id) for connected users.
`_sync_dt_user_id_from_uuid` must be collision-safe: never overwrite an existing
value, and skip a uuid already owned by another telegram_id (unique
`users_dt_user_id_key`) instead of raising.
"""
import os
import sys
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from db.queries.identity import _sync_dt_user_id_from_uuid  # noqa: E402

UUID = "2f95ffde-a9cf-4992-8cc3-f438a5284f05"


@pytest.mark.asyncio
async def test_sync_sets_when_null_and_free():
    """dt_user_id NULL + uuid free → guarded UPDATE matches one row → 'set'."""
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"
    assert await _sync_dt_user_id_from_uuid(conn, 6795592, UUID) == "set"
    conn.fetchrow.assert_not_called()  # no disambiguation needed on success


@pytest.mark.asyncio
async def test_sync_skips_when_already_present():
    """UPDATE matched 0 rows AND dt_user_id already set → 'present' (idempotent)."""
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 0"
    conn.fetchrow.return_value = {"dt_user_id": UUID}
    assert await _sync_dt_user_id_from_uuid(conn, 6795592, UUID) == "present"


@pytest.mark.asyncio
async def test_sync_reports_conflict_when_uuid_owned_by_other():
    """UPDATE matched 0 rows (NOT EXISTS blocked it) AND row still NULL → 'conflict'.

    The diff=1 case (two telegram_ids → one Ory UUID): never overwrite, surface it.
    """
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 0"
    conn.fetchrow.return_value = {"dt_user_id": None}
    assert await _sync_dt_user_id_from_uuid(conn, 403524630, UUID) == "conflict"
