"""Smoke tests for core.nudge_type_registry (WP-117 Ф-roles).

Verifies that every nudge type produced by engagement_analyzer.py is
registered in the WP-418 policy config with the correct cooldown/class_cap.
"""

import core.nudge_delivery as nd
import core.nudge_type_registry as reg


EXPECTED_TYPES = {
    # Basic threshold rules
    ("nudge_slot_missing_3d", 7, nd.ClassCap.CLASS_CAPPED),
    ("nudge_inactivity", 7, nd.ClassCap.CLASS_CAPPED),
    ("nudge_streak_drop", 14, nd.ClassCap.CLASS_CAPPED),
    ("nudge_low_engagement", 14, nd.ClassCap.CLASS_CAPPED),
    ("nudge_marathon_stalled", 7, nd.ClassCap.CLASS_CAPPED),
    # Achievement milestones
    ("nudge_sessions_10", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_sessions_25", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_sessions_50", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_sessions_100", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_active_days_7", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_active_days_14", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_active_days_30", 30, nd.ClassCap.CLASS_CAPPED),
    # Derived-aware rules
    ("nudge_stage_reached_2", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_stage_reached_3", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_stage_reached_4", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_agency_growing", 14, nd.ClassCap.CLASS_CAPPED),
    ("nudge_agency_high", 30, nd.ClassCap.CLASS_CAPPED),
    ("nudge_low_regularity", 14, nd.ClassCap.CLASS_CAPPED),
    ("nudge_reduce_frequency", 30, nd.ClassCap.CLASS_CAPPED),
    # Diagnost bottleneck slots
    ("nudge_bottleneck_cp_rhy", 14, nd.ClassCap.CLASS_CAPPED),
    ("nudge_bottleneck_cp_wld", 14, nd.ClassCap.CLASS_CAPPED),
    ("nudge_bottleneck_cp_skl", 14, nd.ClassCap.CLASS_CAPPED),
    ("nudge_bottleneck_cp_int", 14, nd.ClassCap.CLASS_CAPPED),
    ("nudge_bottleneck_cp_agt", 14, nd.ClassCap.CLASS_CAPPED),
}


def test_registered_types_contains_all_rules():
    types = reg.registered_types()
    assert len(types) == len(EXPECTED_TYPES)
    for nudge_type, cooldown, class_cap in EXPECTED_TYPES:
        config = types[nudge_type]
        assert config.nudge_type == nudge_type
        assert config.cooldown_days == cooldown
        assert config.class_cap == class_cap
        assert config.channel_defaults == ["telegram"]


def test_register_types_merges_into_target():
    target = {}
    reg.register_types(target)
    assert set(target) == {t[0] for t in EXPECTED_TYPES}
    # Default channel preserved
    assert target["nudge_inactivity"].channel_defaults == ["telegram"]
