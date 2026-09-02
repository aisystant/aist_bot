"""
Regression test for db/queries/profile.py::reset_learning_data().

No live DB required: validates the table identifiers statically, the same
way asyncpg would reject a malformed one at execute() time.

Run: python3 -m pytest tests/test_reset_learning_data_tables.py -v
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.queries.profile import reset_learning_data


def test_learning_pool_loop_uses_public_schema_not_learning():
    """Regression test for a 2026-09-02 bug (same class as the fix in
    delete_all_user_data(), found while backporting that fix to pilot):
    feed_weeks/marathon_content/answers/activity_log/assessments physically
    live in the PUBLIC schema of the learning-pool database, not under a
    "learning" schema -- confirmed for this same pool/table set in the
    sibling delete_all_user_data() investigation. The old 'learning.' prefix
    made every DELETE in this loop raise UndefinedTableError, and the
    surrounding try/except only logs a warning -- the caller (states/
    utilities/mydata.py:_reset_learning) reports a false "success" to the
    user with these five tables silently never cleared."""
    source = inspect.getsource(reset_learning_data)
    assert "'learning.' + table" not in source, "old wrong-schema prefix must be gone"
    assert "_delete_from_sql('public.' + table, 'chat_id = $1')" in source
