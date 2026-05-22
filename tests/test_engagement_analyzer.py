"""
Тесты: engagement_analyzer — правила nudge-системы.

Покрывает WP-117 Этап 1: правило slot_missing_3d.
Запуск: python3 tests/test_engagement_analyzer.py
Совместимость: Python 3.9+ (не зависит от aiogram/asyncpg)
"""

import sys
import os
from datetime import date, datetime, timezone, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.engagement_analyzer import analyze, check_slot_missing_3d


def _meta(last_slot_date=None, last_active_date=None, streak=0, longest_streak=0,
          marathon_status=None, active_days_total=0, active_days_streak=0):
    return {
        'last_slot_date': last_slot_date,
        'last_active_date': last_active_date,
        'active_days_streak': active_days_streak,
        'longest_streak': longest_streak,
        'marathon_status': marathon_status,
        'active_days_total': active_days_total,
    }


# ─────────────────────────────────────────────────────────────
# slot_missing_3d
# ─────────────────────────────────────────────────────────────

def test_slot_missing_3d_fires_after_3_days():
    last_slot = datetime.now(timezone.utc) - timedelta(days=4)
    result = check_slot_missing_3d({}, _meta(last_slot_date=last_slot))
    assert result == "nudge_slot_missing_3d", f"Expected nudge_slot_missing_3d, got {result}"


def test_slot_missing_3d_fires_exactly_3_days():
    last_slot = datetime.now(timezone.utc) - timedelta(days=3)
    result = check_slot_missing_3d({}, _meta(last_slot_date=last_slot))
    assert result == "nudge_slot_missing_3d", f"Expected nudge_slot_missing_3d, got {result}"


def test_slot_missing_3d_no_fire_within_3_days():
    last_slot = datetime.now(timezone.utc) - timedelta(days=2)
    result = check_slot_missing_3d({}, _meta(last_slot_date=last_slot))
    assert result is None, f"Expected None for recent slot, got {result}"


def test_slot_missing_3d_no_fire_if_never_logged():
    result = check_slot_missing_3d({}, _meta(last_slot_date=None))
    assert result is None, "Expected None if user never logged slot"


def test_slot_missing_3d_handles_date_object():
    last_slot_date = date.today() - timedelta(days=5)
    result = check_slot_missing_3d({}, _meta(last_slot_date=last_slot_date))
    assert result == "nudge_slot_missing_3d"


def test_slot_missing_3d_handles_string_iso():
    four_days_ago = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    result = check_slot_missing_3d({}, _meta(last_slot_date=four_days_ago))
    assert result == "nudge_slot_missing_3d"


def test_slot_missing_3d_in_full_analyze_pipeline():
    last_slot = datetime.now(timezone.utc) - timedelta(days=4)
    nudges = analyze({}, _meta(last_slot_date=last_slot))
    rule_ids = [n['rule_id'] for n in nudges]
    assert 'slot_missing_3d' in rule_ids, f"slot_missing_3d missing from {rule_ids}"
    nudge_keys = [n['nudge_key'] for n in nudges]
    assert 'nudge_slot_missing_3d' in nudge_keys


def test_slot_missing_3d_not_in_analyze_if_recent():
    last_slot = datetime.now(timezone.utc) - timedelta(days=1)
    nudges = analyze({}, _meta(last_slot_date=last_slot))
    rule_ids = [n['rule_id'] for n in nudges]
    assert 'slot_missing_3d' not in rule_ids


if __name__ == '__main__':
    tests = [
        test_slot_missing_3d_fires_after_3_days,
        test_slot_missing_3d_fires_exactly_3_days,
        test_slot_missing_3d_no_fire_within_3_days,
        test_slot_missing_3d_no_fire_if_never_logged,
        test_slot_missing_3d_handles_date_object,
        test_slot_missing_3d_handles_string_iso,
        test_slot_missing_3d_in_full_analyze_pipeline,
        test_slot_missing_3d_not_in_analyze_if_recent,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} PASS")
    if failed:
        sys.exit(1)
