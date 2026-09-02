"""
Regression test for db/queries/dev_stats.py::get_table_sizes().

No live DB required: validates the SQL table identifiers statically, the
same way asyncpg would reject them at execute() time if malformed.

Run: python3 -m pytest tests/test_dev_stats_table_sizes.py -v
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.queries.dev_stats import get_table_sizes


def test_learning_pool_loop_uses_public_schema_not_learning():
    """Regression test for a 2026-09-02 bug (same class as the fix already
    applied to delete_all_user_data()/reset_learning_data() the same day):
    answers/activity_log/assessments physically live in the PUBLIC schema of
    the learning-pool database, not under a "learning" schema. The old
    'learning.' prefix made every count in this loop raise, silently falling
    back to count=-1 (a TD1-only dev diagnostic, not user-facing, but still
    wrong)."""
    source = inspect.getsource(get_table_sizes)
    learning_block = source[source.index("for table in ['answers'"):source.index("for table in ['error_logs'")]
    assert "'learning.' + table" not in learning_block, "old wrong-schema prefix must be gone"
    assert "select_count_from('public.' + table)" in learning_block


def test_health_pool_loop_uses_public_schema_not_health():
    """Same bug, fourth instance found by repo-wide grep for the same
    concatenation pattern (2026-09-02): error_logs/user_sessions/
    pending_fixes physically live in PUBLIC schema of the health-pool
    database, not under a "health" schema."""
    source = inspect.getsource(get_table_sizes)
    health_block = source[source.index("for table in ['error_logs'"):]
    assert "'health.' + table" not in health_block, "old wrong-schema prefix must be gone"
    assert "select_count_from('public.' + table)" in health_block


def test_display_labels_kept_as_pool_group_not_changed():
    """The display label (what the TD1 dev command shows) is intentionally
    NOT changed by this fix -- it groups results by which connection pool
    served them, not by the real SQL schema, and nothing downstream parses
    this exact string (verified by repo-wide grep before this fix)."""
    source = inspect.getsource(get_table_sizes)
    assert "f'learning.{table}'" in source
    assert "f'health.{table}'" in source


def test_marathon_queue_and_state_still_use_learning_schema():
    """Regression guard against a well-intentioned but wrong follow-up fix:
    learning.marathon_queue and learning.marathon_state (a DIFFERENT function
    in this module) are confirmed live (to_regclass, production) to
    genuinely exist under the learning schema, unlike the tables fixed
    above. Nobody should "clean up" that prefix too."""
    import db.queries.dev_stats as dev_stats_module
    source = inspect.getsource(dev_stats_module)
    assert "FROM learning.marathon_queue" in source
    assert "FROM learning.marathon_state" in source
