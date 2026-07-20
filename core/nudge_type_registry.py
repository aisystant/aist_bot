"""Nudge type registry for WP-117 Ф-roles.

Registers all nudge types produced by engagement_analyzer.py in the
WP-418 policy engine (core.nudge_delivery.NUDGE_TYPE_CONFIG).

This is a transitional step: the legacy scheduler still routes engagement
nudges through notification_service.enqueue(), but the new NudgeProducer
will use core.nudge_delivery.select_and_enqueue() once wired.

Boundary:
- WP-117 owns the rule/taxonomy and this registry.
- WP-418 owns cooldown, class cap, opt-out, channel preferences.
"""

from __future__ import annotations

from core.nudge_delivery import ClassCap, NudgeTypeConfig

# Cooldowns mirror engagement_analyzer.RULES and DERIVED_RULES.
# All engagement nudges are CLASS_CAPPED because they are optional,
# source-agnostic outreach subject to the global daily cap (DP.SC.177).
_REGISTERED: dict[str, NudgeTypeConfig] = {
    # ── Basic threshold rules ─────────────────────────────────────────
    "nudge_slot_missing_3d": NudgeTypeConfig(
        nudge_type="nudge_slot_missing_3d",
        cooldown_days=7,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_inactivity": NudgeTypeConfig(
        nudge_type="nudge_inactivity",
        cooldown_days=7,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_streak_drop": NudgeTypeConfig(
        nudge_type="nudge_streak_drop",
        cooldown_days=14,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_low_engagement": NudgeTypeConfig(
        nudge_type="nudge_low_engagement",
        cooldown_days=14,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_marathon_stalled": NudgeTypeConfig(
        nudge_type="nudge_marathon_stalled",
        cooldown_days=7,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    # Achievement milestones
    "nudge_sessions_10": NudgeTypeConfig(
        nudge_type="nudge_sessions_10",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_sessions_25": NudgeTypeConfig(
        nudge_type="nudge_sessions_25",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_sessions_50": NudgeTypeConfig(
        nudge_type="nudge_sessions_50",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_sessions_100": NudgeTypeConfig(
        nudge_type="nudge_sessions_100",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_active_days_7": NudgeTypeConfig(
        nudge_type="nudge_active_days_7",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_active_days_14": NudgeTypeConfig(
        nudge_type="nudge_active_days_14",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_active_days_30": NudgeTypeConfig(
        nudge_type="nudge_active_days_30",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    # ── Derived-aware rules ───────────────────────────────────────────
    "nudge_stage_reached_2": NudgeTypeConfig(
        nudge_type="nudge_stage_reached_2",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_stage_reached_3": NudgeTypeConfig(
        nudge_type="nudge_stage_reached_3",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_stage_reached_4": NudgeTypeConfig(
        nudge_type="nudge_stage_reached_4",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_agency_growing": NudgeTypeConfig(
        nudge_type="nudge_agency_growing",
        cooldown_days=14,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_agency_high": NudgeTypeConfig(
        nudge_type="nudge_agency_high",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_low_regularity": NudgeTypeConfig(
        nudge_type="nudge_low_regularity",
        cooldown_days=14,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_reduce_frequency": NudgeTypeConfig(
        nudge_type="nudge_reduce_frequency",
        cooldown_days=30,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    # Onboarder gap (WP-406 Х2/Х3 не закрыты)
    "nudge_onboarder_gap_x2": NudgeTypeConfig(
        nudge_type="nudge_onboarder_gap_x2",
        cooldown_days=7,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_onboarder_gap_x3": NudgeTypeConfig(
        nudge_type="nudge_onboarder_gap_x3",
        cooldown_days=7,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    # Diagnost bottleneck slots (cp-profile)
    "nudge_bottleneck_cp_rhy": NudgeTypeConfig(
        nudge_type="nudge_bottleneck_cp_rhy",
        cooldown_days=14,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_bottleneck_cp_wld": NudgeTypeConfig(
        nudge_type="nudge_bottleneck_cp_wld",
        cooldown_days=14,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_bottleneck_cp_skl": NudgeTypeConfig(
        nudge_type="nudge_bottleneck_cp_skl",
        cooldown_days=14,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_bottleneck_cp_int": NudgeTypeConfig(
        nudge_type="nudge_bottleneck_cp_int",
        cooldown_days=14,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
    "nudge_bottleneck_cp_agt": NudgeTypeConfig(
        nudge_type="nudge_bottleneck_cp_agt",
        cooldown_days=14,
        class_cap=ClassCap.CLASS_CAPPED,
    ),
}


def registered_types() -> dict[str, NudgeTypeConfig]:
    """Return a shallow copy of the registry."""
    return dict(_REGISTERED)


def register_types(target_config: dict[str, NudgeTypeConfig] | None = None) -> dict[str, NudgeTypeConfig]:
    """Merge the WP-117 registry into a target config dict.

    If target_config is omitted, merges into core.nudge_delivery.NUDGE_TYPE_CONFIG.
    """
    if target_config is None:
        from core.nudge_delivery import NUDGE_TYPE_CONFIG
        target_config = NUDGE_TYPE_CONFIG
    target_config.update(_REGISTERED)
    return target_config
