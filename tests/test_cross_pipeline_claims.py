"""
Тесты: core.claims — кросс-конвейерный арбитр (WP-117 Ф-cross-pipeline-contradiction).

Покрывает S3 (should_suppress_cross_pipeline) + вспомогательные чистые функции
(_milestone_polarity, _as_aware_utc). Без обращения к БД — get_recent_claims()
(async, две БД) не юнит-тестируется здесь, только его чистая часть решения.

Запуск: python3 tests/test_cross_pipeline_claims.py
"""

import sys
import os
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.claims import (
    should_suppress_cross_pipeline,
    filter_cross_pipeline_candidates,
    _milestone_polarity,
    _as_aware_utc,
)
from core.activity_sources import LIVE_SOURCE_RULES, DIGITAL_TWIN_SNAPSHOT_RULES


def _claim(claim="high_activity", hours_ago=26, source="milestone"):
    return {
        "source": source,
        "claim": claim,
        "observed_at": datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    }


# ─────────────────────────────────────────────────────────────
# should_suppress_cross_pipeline — ядро S3
# ─────────────────────────────────────────────────────────────

def test_live_source_rule_not_suppressed_by_recent_praise():
    """Инцидент 02-03.10 не повторится: правило из увеличения 1 (живой
    источник) само является самым свежим наблюдением — недавняя похвала
    (milestone 26ч назад) его не подавляет."""
    claims = [_claim(claim="high_activity")]
    result = should_suppress_cross_pipeline("low_engagement_7d", claims)
    assert result is False, f"Expected live-source rule not suppressed, got {result}"


def test_twin_source_rule_suppressed_by_recent_praise():
    """Правило без доказанной свежести (ещё не переведено на живой источник,
    S1-шаг-2) — подавляется при недавней похвале (S2 fail-closed)."""
    claims = [_claim(claim="high_activity")]
    result = should_suppress_cross_pipeline("achievement_sessions", claims)
    assert result is True, f"Expected twin-source rule suppressed, got {result}"


def test_no_recent_claims_never_suppresses():
    result = should_suppress_cross_pipeline("achievement_sessions", [])
    assert result is False, f"Expected no suppression without claims, got {result}"


def test_recent_low_activity_claim_does_not_suppress():
    """Недавняя похвала подавляет; недавний УПРЁК — не повод подавлять
    следующий упрёк (это не противоречие, это согласие)."""
    claims = [_claim(claim="low_activity")]
    result = should_suppress_cross_pipeline("achievement_sessions", claims)
    assert result is False, f"Expected no suppression from a low_activity claim, got {result}"


def test_all_live_source_rules_exempt():
    for rule_id in LIVE_SOURCE_RULES:
        claims = [_claim(claim="high_activity")]
        assert should_suppress_cross_pipeline(rule_id, claims) is False, (
            f"Expected {rule_id} (live source) to be exempt from suppression"
        )


def test_all_twin_snapshot_rules_suppressible():
    for rule_id in DIGITAL_TWIN_SNAPSHOT_RULES:
        claims = [_claim(claim="high_activity")]
        assert should_suppress_cross_pipeline(rule_id, claims) is True, (
            f"Expected {rule_id} (twin snapshot) to be suppressible"
        )


# ─────────────────────────────────────────────────────────────
# filter_cross_pipeline_candidates — фильтр применяется только к
# reactivation, не к recognition в том же тике (регрессия ревью
# 08-peer-kimi.md)
# ─────────────────────────────────────────────────────────────

def _canonical_type_of(rule_id):
    if rule_id in ("low_engagement_7d", "inactivity_3d", "streak_drop", "achievement_sessions"):
        return "engagement_reactivation" if rule_id not in DIGITAL_TWIN_SNAPSHOT_RULES else "recognition_progress"
    return "recognition_progress"


def test_filter_suppresses_only_twin_reactivation_not_recognition_in_same_tick():
    """Смешанный тик: упрёк из не-live правила + похвала в том же тике.
    should_suppress_cross_pipeline сама по себе не различает reactivation от
    recognition — filter_cross_pipeline_candidates обязана не пропускать
    recognition-кандидата через неё вовсе."""
    nudges = [
        {"rule_id": "achievement_sessions", "nudge_key": "nudge_sessions_10"},  # recognition, twin
        {"rule_id": "low_engagement_7d", "nudge_key": "nudge_low_engagement"},  # reactivation, live
    ]
    claims = [_claim(claim="high_activity")]
    result = filter_cross_pipeline_candidates(nudges, claims, _canonical_type_of)
    rule_ids = {n["rule_id"] for n in result}
    assert "achievement_sessions" in rule_ids, "Recognition candidate must not be suppressed by its own praise"
    assert "low_engagement_7d" in rule_ids, "Live-source reactivation must not be suppressed either"


def test_filter_suppresses_twin_reactivation_candidate():
    """Мок: rule_id вне LIVE_SOURCE_RULES, но canonical_type помечен как
    reactivation (гипотетическое будущее правило без доказанной свежести) —
    должен подавляться."""
    nudges = [{"rule_id": "achievement_sessions", "nudge_key": "nudge_sessions_10"}]
    claims = [_claim(claim="high_activity")]

    def canonical(rule_id):
        return "engagement_reactivation"

    result = filter_cross_pipeline_candidates(nudges, claims, canonical)
    assert result == [], f"Expected reactivation candidate suppressed, got {result}"


def test_filter_no_claims_returns_nudges_unchanged():
    nudges = [{"rule_id": "low_engagement_7d", "nudge_key": "nudge_low_engagement"}]
    result = filter_cross_pipeline_candidates(nudges, [], _canonical_type_of)
    assert result == nudges


# ─────────────────────────────────────────────────────────────
# _milestone_polarity — разбор action из conversion_event
# ─────────────────────────────────────────────────────────────

def test_milestone_polarity_active():
    assert _milestone_polarity("shown:active") == "active"


def test_milestone_polarity_inactive():
    assert _milestone_polarity("shown:inactive") == "inactive"


def test_milestone_polarity_legacy_shown_is_none():
    """Старые записи (до этой фазы) без полярности — claims.py их пропускает,
    не выдумывая факт из отсутствующих данных."""
    assert _milestone_polarity("shown") is None


def test_milestone_polarity_none_action_is_none():
    assert _milestone_polarity(None) is None


def test_milestone_polarity_unknown_suffix_is_none():
    assert _milestone_polarity("shown:clicked") is None


# ─────────────────────────────────────────────────────────────
# _as_aware_utc — normalize naive/aware datetime перед сравнением
# ─────────────────────────────────────────────────────────────

def test_as_aware_utc_naive_input_gets_utc_tzinfo():
    naive = datetime(2026, 10, 3, 12, 0, 0)
    result = _as_aware_utc(naive)
    assert result.tzinfo is not None, "Expected tzinfo attached to naive input"
    assert result.utcoffset().total_seconds() == 0


def test_as_aware_utc_aware_input_converted_to_utc():
    aware_plus3 = datetime(2026, 10, 3, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    result = _as_aware_utc(aware_plus3)
    assert result.hour == 12, f"Expected 12:00 UTC from 15:00 MSK, got {result.hour}"


def test_as_aware_utc_mixed_sources_comparable():
    """Regression: naive (conversion_event, вероятно TIMESTAMP) и aware
    (domain_event, вероятно TIMESTAMPTZ) не должны ронять TypeError при
    сравнении после нормализации — именно тот класс бага, что уже был на
    МСК/UTC (CLAUDE.md §10.6)."""
    naive = datetime(2026, 10, 3, 10, 0, 0)
    aware = datetime(2026, 10, 3, 11, 0, 0, tzinfo=timezone.utc)
    a, b = _as_aware_utc(naive), _as_aware_utc(aware)
    assert a < b  # не должно упасть TypeError


if __name__ == '__main__':
    tests = [
        test_live_source_rule_not_suppressed_by_recent_praise,
        test_twin_source_rule_suppressed_by_recent_praise,
        test_no_recent_claims_never_suppresses,
        test_recent_low_activity_claim_does_not_suppress,
        test_all_live_source_rules_exempt,
        test_all_twin_snapshot_rules_suppressible,
        test_filter_suppresses_only_twin_reactivation_not_recognition_in_same_tick,
        test_filter_suppresses_twin_reactivation_candidate,
        test_filter_no_claims_returns_nudges_unchanged,
        test_milestone_polarity_active,
        test_milestone_polarity_inactive,
        test_milestone_polarity_legacy_shown_is_none,
        test_milestone_polarity_none_action_is_none,
        test_milestone_polarity_unknown_suffix_is_none,
        test_as_aware_utc_naive_input_gets_utc_tzinfo,
        test_as_aware_utc_aware_input_converted_to_utc,
        test_as_aware_utc_mixed_sources_comparable,
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
