"""
Тесты: core.activity_sources — реестр источников факта (WP-117 S4).

Запуск: python3 tests/test_activity_source_registry.py
"""

import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import core.engagement_analyzer as analyzer
from core.activity_sources import LIVE_SOURCE_RULES, DIGITAL_TWIN_SNAPSHOT_RULES


def test_live_and_twin_registries_disjoint():
    """Одно правило не может одновременно быть «живым» и «снимком твина»."""
    overlap = LIVE_SOURCE_RULES & DIGITAL_TWIN_SNAPSHOT_RULES
    assert not overlap, f"Expected disjoint registries, overlap: {overlap}"


def test_live_source_rules_are_registered_analyzer_rules():
    """Реестр не должен ссылаться на несуществующие rule_id — иначе
    should_suppress_cross_pipeline тихо экранирует правило, которого нет."""
    known_rule_ids = {rule_id for rule_id, _fn, _cooldown in analyzer.RULES}
    for rule_id in LIVE_SOURCE_RULES:
        assert rule_id in known_rule_ids, f"{rule_id} not found in analyzer.RULES"


def test_known_stopgapped_rules_are_declared_as_twin_snapshot():
    """WP-117 stopgap (nudge_policy.py) отключает achievement_* и
    stage_upgrade именно из-за снимка твина — реестр источников должен
    согласованно их помечать, иначе при снятии stopgap они пройдут арбитр
    без защиты."""
    for rule_id in ("achievement_sessions", "achievement_active_days", "stage_upgrade"):
        assert rule_id in DIGITAL_TWIN_SNAPSHOT_RULES, (
            f"{rule_id} expected in DIGITAL_TWIN_SNAPSHOT_RULES"
        )


if __name__ == '__main__':
    tests = [
        test_live_and_twin_registries_disjoint,
        test_live_source_rules_are_registered_analyzer_rules,
        test_known_stopgapped_rules_are_declared_as_twin_snapshot,
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
