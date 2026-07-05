"""
Тесты: engagement_analyzer — правила nudge-системы.

Покрывает WP-117 Этап 1: правило slot_missing_3d.
Покрывает WP-117 Ф-roles: МСК/UTC off-by-one (инцидент 21 апр), marathon_status
guard, diagnost_bottleneck.
Запуск: python3 tests/test_engagement_analyzer.py
Совместимость: Python 3.9+ (не зависит от aiogram/asyncpg)
"""

import sys
import os
from datetime import date, datetime, timezone, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from config import MOSCOW_TZ
from core.engagement_analyzer import (
    analyze,
    check_slot_missing_3d,
    check_inactivity_3d,
    check_marathon_stalled,
    check_diagnost_bottleneck,
)


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


# ─────────────────────────────────────────────────────────────
# WP-117 Ф-roles: МСК/UTC off-by-one (инцидент 21 апр)
# ─────────────────────────────────────────────────────────────

def test_marathon_stalled_skips_completed():
    """Guard marathon_status != 'active' — завершённый марафон не получает stalled."""
    meta = _meta(last_active_date=date.today() - timedelta(days=10),
                  marathon_status='completed')
    result = check_marathon_stalled({}, meta)
    assert result is None, f"Expected None for completed marathon, got {result}"


def test_marathon_stalled_no_false_positive_near_msk_midnight():
    """Инцидент 21 апр: активность в 00:06 МСК не должна давать days_inactive >= 3.

    last_active_date пишется через moscow_today() — здесь имитируем «только что
    наступила МСК-полночь» и берём МСК-дату напрямую (то, что реально попадёт в БД).
    """
    msk_now = datetime.now(MOSCOW_TZ)
    last_active_msk_date = msk_now.date()  # это пишет touch_last_active_date() прямо сейчас
    meta = _meta(last_active_date=last_active_msk_date, marathon_status='active')
    result = check_marathon_stalled({}, meta)
    assert result is None, (
        f"False positive: активность только что (МСК {msk_now.isoformat()}), "
        f"но правило сработало: {result}"
    )


def test_inactivity_3d_uses_msk_calendar():
    """inactivity_3d должен считать 'сегодня' по МСК, не по UTC."""
    msk_today = datetime.now(MOSCOW_TZ).date()
    meta = _meta(last_active_date=msk_today)
    result = check_inactivity_3d({}, meta)
    assert result is None, f"Активность сегодня (МСК) не должна давать inactivity: {result}"


def test_slot_missing_3d_converts_utc_timestamptz_to_msk_date():
    """last_slot_at из БД — TIMESTAMPTZ (aware UTC). Ровно 3 МСК-суток назад → срабатывает."""
    three_msk_days_ago = datetime.now(MOSCOW_TZ) - timedelta(days=3)
    last_slot_utc = three_msk_days_ago.astimezone(timezone.utc)
    result = check_slot_missing_3d({}, _meta(last_slot_date=last_slot_utc))
    assert result == "nudge_slot_missing_3d", f"Expected fire at exactly 3 MSK days, got {result}"


# ─────────────────────────────────────────────────────────────
# WP-117 Ф-roles: diagnost_bottleneck
# ─────────────────────────────────────────────────────────────

def test_diagnost_bottleneck_fires_with_cp_profile():
    derived = {'_cp_profile': {'bottleneck_slot': 'cp.skl', 'recommended_stream': 'S2'}}
    result = check_diagnost_bottleneck({}, _meta(), derived)
    assert isinstance(result, dict), f"Expected dict payload, got {result}"
    assert result['nudge_type'] == 'nudge_bottleneck_cp_skl'
    assert result['bottleneck_slot'] == 'cp.skl'


def test_diagnost_bottleneck_none_when_no_bottleneck():
    """bottleneck_slot='none' (WP-370: stage >= 4) — нет узкого места, не нудить."""
    derived = {'_cp_profile': {'bottleneck_slot': 'none', 'recommended_stream': 'РР'}}
    result = check_diagnost_bottleneck({}, _meta(), derived)
    assert result is None, f"Expected None for bottleneck='none', got {result}"


def test_diagnost_bottleneck_none_without_cp_profile():
    """Нет cp-профиля (пользователь не проходил диагностику) — правило молчит."""
    result = check_diagnost_bottleneck({}, _meta(), {})
    assert result is None, f"Expected None without _cp_profile, got {result}"


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
        test_marathon_stalled_skips_completed,
        test_marathon_stalled_no_false_positive_near_msk_midnight,
        test_inactivity_3d_uses_msk_calendar,
        test_slot_missing_3d_converts_utc_timestamptz_to_msk_date,
        test_diagnost_bottleneck_fires_with_cp_profile,
        test_diagnost_bottleneck_none_when_no_bottleneck,
        test_diagnost_bottleneck_none_without_cp_profile,
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
