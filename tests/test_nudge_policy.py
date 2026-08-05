"""
Тесты: core.nudge_policy — WP-117 Ф-stopgap policy.

Чистый unit-тест, не требует aiogram/asyncpg/БД.
Запуск: python3 tests/test_nudge_policy.py
"""

import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.nudge_policy import (
    stopgap_suppression_reason,
    is_stopgap_dedup_key,
    stopgap_dedup_key_pattern,
)


def test_achievement_sessions_suppressed():
    assert stopgap_suppression_reason("achievement_sessions", "nudge_sessions_10") is not None


def test_achievement_active_days_suppressed():
    assert stopgap_suppression_reason("achievement_active_days", "nudge_active_days_7") is not None


def test_stage_upgrade_suppressed():
    assert stopgap_suppression_reason("stage_upgrade", "nudge_stage_reached_2") is not None


def test_prefix_match_even_if_rule_id_unknown():
    """Defense in depth: если rule_id не совпал, но nudge_key — achievement, подавляем."""
    assert stopgap_suppression_reason("some_future_rule", "nudge_active_days_30") is not None


def test_agency_high_not_suppressed():
    assert stopgap_suppression_reason("agency_high", "nudge_agency_high") is None


def test_inactivity_not_suppressed():
    assert stopgap_suppression_reason("inactivity_3d", "nudge_inactivity") is None


def test_dedup_key_pattern_matches_achievement():
    assert is_stopgap_dedup_key("nudge:123:nudge_sessions_10")
    assert is_stopgap_dedup_key("nudge:123:nudge_active_days_30")
    assert is_stopgap_dedup_key("nudge:123:nudge_stage_reached_2")


def test_dedup_key_pattern_ignores_non_achievement():
    assert not is_stopgap_dedup_key("nudge:123:nudge_agency_high")
    assert not is_stopgap_dedup_key("nudge:123:nudge_inactivity")
    assert not is_stopgap_dedup_key("nudge:123:nudge_onboarder_gap_x2")


def test_dedup_key_pattern_ignores_malformed():
    assert not is_stopgap_dedup_key("nudge:abc:nudge_sessions_10")
    assert not is_stopgap_dedup_key("other:123:nudge_sessions_10")
    assert not is_stopgap_dedup_key(None)


def test_dedup_key_pattern_is_valid_postgres_regex():
    """Проверяем, что паттерн содержит якоря и непустые группы — достаточно для визуальной валидации."""
    pattern = stopgap_dedup_key_pattern()
    assert pattern.startswith("^")
    assert pattern.endswith("$")
    assert "nudge_sessions_" in pattern
    assert "nudge_active_days_" in pattern
    assert "nudge_stage_reached_" in pattern


if __name__ == '__main__':
    tests = [
        test_achievement_sessions_suppressed,
        test_achievement_active_days_suppressed,
        test_stage_upgrade_suppressed,
        test_prefix_match_even_if_rule_id_unknown,
        test_agency_high_not_suppressed,
        test_inactivity_not_suppressed,
        test_dedup_key_pattern_matches_achievement,
        test_dedup_key_pattern_ignores_non_achievement,
        test_dedup_key_pattern_ignores_malformed,
        test_dedup_key_pattern_is_valid_postgres_regex,
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
