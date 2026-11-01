"""Unit-тест WP-7 Ф121 (DP.SC.162 §close): report.md должен нести
tg_chat_id/target_bot/executor из meta, не только session_id/date/topic.

До этой правки _build_report_md читал только turn_count и created_at из
SESSION-<id>.md — три поля архивных метаданных терялись при финализации,
решение пилота (DP.SC.162 §close) переносит их прямо во frontmatter report.md
вместо отдельного session.md.
"""

import os
import re
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from handlers.external_session import _build_report_md  # noqa: E402

META_TEXT = (
    "---\n"
    "session_id: SESSION-20260930-120000-ab12cd\n"
    "tg_chat_id: -100123456789\n"
    "target_bot: aist_pilot_bot\n"
    "executor: kimi\n"
    "created_at: 2026-09-30T12:00:00Z\n"
    "last_turn_at: 2026-09-30T12:05:00Z\n"
    "status: completed\n"
    "private: false\n"
    "turn_count: 3\n"
    "---\n"
)


def _frontmatter_field(report_md: str, key: str) -> str:
    match = re.search(rf"^{key}:\s*(.+)$", report_md, re.M)
    assert match, f"{key} missing from report.md frontmatter:\n{report_md}"
    return match.group(1).strip()


def test_report_frontmatter_carries_meta_fields():
    report_md = _build_report_md(
        "SESSION-20260930-120000-ab12cd", META_TEXT, "thread body", "topic-slug"
    )
    assert _frontmatter_field(report_md, "tg_chat_id") == "-100123456789"
    assert _frontmatter_field(report_md, "target_bot") == "aist_pilot_bot"
    assert _frontmatter_field(report_md, "executor") == "kimi"
    # Pre-existing fields must still be there, unchanged.
    assert _frontmatter_field(report_md, "turns") == "3"
    assert _frontmatter_field(report_md, "session_id") == "SESSION-20260930-120000-ab12cd"


def test_report_frontmatter_defaults_to_null_without_meta_fields():
    """A meta file from before this fields existed must not crash the build."""
    old_meta = (
        "---\n"
        "session_id: SESSION-20260601-000000-aaaaaa\n"
        "created_at: 2026-06-01T00:00:00Z\n"
        "turn_count: 1\n"
        "---\n"
    )
    report_md = _build_report_md(
        "SESSION-20260601-000000-aaaaaa", old_meta, "thread body", None
    )
    assert _frontmatter_field(report_md, "tg_chat_id") == "null"
    assert _frontmatter_field(report_md, "target_bot") == "null"
    assert _frontmatter_field(report_md, "executor") == "null"
