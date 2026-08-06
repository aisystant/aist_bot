"""
Tests for db/queries/profile.py — OPTIONAL_CHAT_TABLES used by delete_all_user_data().

No live DB required: validates the table/column identifiers statically, the
same way asyncpg would reject them at execute() time if malformed.

Run: python3 -m pytest tests/test_delete_all_user_data_tables.py -v
"""

import inspect
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.queries.profile import OPTIONAL_CHAT_TABLES, delete_all_user_data
from db.sql_helpers import delete_from


def test_every_entry_builds_valid_sql():
    for table, column in OPTIONAL_CHAT_TABLES:
        sql = delete_from(table, f"{column} = $1")
        assert sql == f"DELETE FROM {table} WHERE {column} = $1"


def test_no_duplicate_tables():
    tables = [table for table, _column in OPTIONAL_CHAT_TABLES]
    assert len(tables) == len(set(tables))


def test_community_members_filters_by_telegram_id_not_chat_id():
    """community_members.chat_id is the GROUP chat (db/queries/workshop.py:
    log_community_join/get_community_stats), not the member's own id — the
    member is telegram_id. Filtering by chat_id=<own id> matches zero rows
    (Telegram group ids are negative, user ids positive): a silent no-op
    that leaves the member's row (username, first_name) undeleted."""
    mapping = dict(OPTIONAL_CHAT_TABLES)
    assert mapping['community_members'] == 'telegram_id'


def test_channel_monitors_handled_in_core_transaction_not_optional_block():
    """channel_monitors has a FK on public.users(id) (db/models.py) and must
    be deleted inside the same core transaction, strictly before the users
    DELETE — not here, where per-table failures are caught and swallowed."""
    tables = [table for table, _column in OPTIONAL_CHAT_TABLES]
    assert 'channel_monitors' not in tables


def test_cross_pool_tables_keyed_by_account_id_not_chat_id():
    """subscription.contract, indicators.calculated_profile and
    rewards.point_balances key by the Ory account UUID (core/access.py,
    core/tier_detector.py, db/queries/rewards.py) — not by chat_id. account_id
    is a uuid column; binding a plain int chat_id to it raises at the asyncpg
    parameter-encoding step, so a chat_id-based DELETE would always no-op
    silently (caught, logged as a warning, reported as 0 rows) — the same
    failure shape as the community_members bug above, confirmed live in a
    peer-session code review with Kimi 2026-08-06 (session
    2026-08-06-16-aist-bot-delete-bugs)."""
    source = inspect.getsource(delete_all_user_data)
    assert "DELETE FROM contract WHERE account_id = $1" in source
    assert "DELETE FROM public.calculated_profile WHERE account_id = $1::uuid" in source
    assert "DELETE FROM point_balances WHERE account_id = $1" in source


def test_club_account_keyed_by_chat_id_directly():
    """club_account (community pool) is the WP-253 successor of discourse_accounts
    (main pool, in OPTIONAL_CHAT_TABLES above) and — unlike the three tables in
    test_cross_pool_tables_keyed_by_account_id_not_chat_id — keys by chat_id
    directly, no account_id resolution needed (db/queries/discourse.py)."""
    source = inspect.getsource(delete_all_user_data)
    assert "DELETE FROM club_account WHERE chat_id = $1" in source


def test_wp253_migrated_pools_not_missed():
    """discourse_accounts/published_posts/scheduled_publications/conversion_events/
    training_settings/training_children/training_progress/training_attempts all have
    WP-253 successor tables in dedicated pools (publication/lead/reference/learning) —
    found by a cold-context code review that caught what both the writer and Kimi
    missed in the peer session: a live child's name (training_child) surviving
    "delete all my data". Confirmed against db/queries/conversion.py,
    db/queries/training.py and db/queries/discourse.py module docstrings."""
    source = inspect.getsource(delete_all_user_data)
    assert "DELETE FROM published_post WHERE chat_id = $1" in source
    assert "DELETE FROM scheduled_post WHERE chat_id = $1" in source
    assert "DELETE FROM conversion_event WHERE chat_id = $1" in source
    assert "DELETE FROM training_setting WHERE chat_id = $1" in source
    assert "DELETE FROM training_child WHERE chat_id = $1" in source
    assert "'learning.' + table" in source
    assert "'training_progress', 'training_attempt'" in source
