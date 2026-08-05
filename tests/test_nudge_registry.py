"""Tests for config.nudge_registry — canonical WP-117 nudge taxonomy.

This is a hard completeness gate: any new rule_id produced by
engagement_analyzer or nudge_key registered in nudge_type_registry must
be explicitly mapped in config/nudge_registry.yaml.
"""

from __future__ import annotations

import core.engagement_analyzer as analyzer
import core.nudge_delivery as delivery
import core.nudge_policy as policy
import core.nudge_type_registry as type_reg
from config.nudge_registry import (
    load_nudge_registry,
    get_rule_config,
    get_nudge_key_config,
    get_canonical_type_for_dp_sc_116_class,
)


# DP.SC.116 declared nudge classes. Every class must map to exactly one
# canonical_type. See PACK-digital-platform/08-service-clauses/DP.SC.116.
DP_SC_116_CLASSES = frozenset({
    "inactivity",
    "streak",
    "milestone",
    "cp_insight",
    "trial",
    "onboarding",
    "fatigue",
    "homework_deadline",
    "homework_reviewed",
    "schedule",
    "mentor",
})

# Canonical types produced by the 7-type taxonomy.
CANONICAL_TYPES = frozenset({
    "engagement_reactivation",
    "practice_rhythm",
    "recognition_progress",
    "diagnostic_insight",
    "onboarding_lifecycle",
    "frequency_adaptation",
    "transactional_learning",
})


def _collect_analyzer_rule_ids() -> set[str]:
    """Return every rule_id registered by engagement_analyzer decorators."""
    return {rule_id for rule_id, _fn, _cooldown in analyzer.RULES + analyzer.DERIVED_RULES}


def _collect_registered_nudge_keys() -> set[str]:
    """Return every nudge_key registered in nudge_type_registry."""
    return set(type_reg.registered_types().keys())


def _class_cap_name(nudge_key: str) -> str:
    config = type_reg.registered_types()[nudge_key]
    return config.class_cap.name.lower()


def test_registry_loads_without_errors():
    registry = load_nudge_registry()
    assert registry.version
    assert registry.rules
    assert registry.nudge_keys


def test_every_analyzer_rule_id_is_mapped():
    """Fail if a new rule is added to engagement_analyzer without registry mapping."""
    registry = load_nudge_registry()
    rule_ids = _collect_analyzer_rule_ids()
    mapped = set(registry.rules.keys())
    missing = rule_ids - mapped
    assert not missing, (
        f"rule_id(s) from engagement_analyzer missing in nudge registry: {sorted(missing)}. "
        f"Add them to config/nudge_registry.yaml."
    )


def test_every_registered_nudge_key_is_mapped():
    """Fail if a new nudge_key is registered in nudge_type_registry without mapping."""
    registry = load_nudge_registry()
    registered = _collect_registered_nudge_keys()
    mapped = set(registry.nudge_keys.keys())
    missing = registered - mapped
    assert not missing, (
        f"nudge_key(s) missing in nudge registry: {sorted(missing)}. "
        f"Add them under the producing rule_id in config/nudge_registry.yaml."
    )


def test_nudge_keys_belong_to_their_rule_id():
    """Every mapped nudge_key must be declared under the rule that produces it."""
    registry = load_nudge_registry()
    for nudge_key, key_config in registry.nudge_keys.items():
        rule = registry.rules[key_config.rule_id]
        assert nudge_key in rule.nudge_keys, (
            f"nudge_key '{nudge_key}' mapped to rule '{key_config.rule_id}' "
            f"but that rule declares {rule.nudge_keys}"
        )


def test_cooldown_and_class_cap_match_nudge_type_registry():
    """Registry data_contract must agree with the runtime NudgeTypeConfig."""
    registry = load_nudge_registry()
    for nudge_key, type_config in type_reg.registered_types().items():
        key_config = registry.nudge_keys[nudge_key]
        assert key_config.data_contract.cooldown_days == type_config.cooldown_days, (
            f"nudge_key '{nudge_key}': cooldown mismatch: "
            f"registry={key_config.data_contract.cooldown_days} "
            f"type_registry={type_config.cooldown_days}"
        )
        expected_cap = type_config.class_cap.name.lower()
        assert key_config.data_contract.class_cap == expected_cap, (
            f"nudge_key '{nudge_key}': class_cap mismatch: "
            f"registry={key_config.data_contract.class_cap} expected={expected_cap}"
        )


def test_canonical_type_matches_rule_category():
    """opt_out_category in registry must match _RULE_CATEGORY in nudge_producer."""
    from core.nudge_producer import _RULE_CATEGORY, _DEFAULT_CATEGORY

    registry = load_nudge_registry()
    for rule_id in _collect_analyzer_rule_ids():
        rule = registry.rules[rule_id]
        expected_category = _RULE_CATEGORY.get(rule_id, _DEFAULT_CATEGORY)
        assert rule.opt_out_category == expected_category, (
            f"rule_id '{rule_id}': opt_out_category mismatch: "
            f"registry={rule.opt_out_category} producer={expected_category}"
        )


def test_stopgap_flags_match_nudge_policy():
    """Registry stopgap flag must agree with nudge_policy stopgap lists."""
    registry = load_nudge_registry()
    for rule_id in _collect_analyzer_rule_ids():
        rule = registry.rules[rule_id]
        expected_stopgap = rule_id in policy.STOPGAP_DISABLED_RULES
        assert rule.data_contract.stopgap == expected_stopgap, (
            f"rule_id '{rule_id}': stopgap mismatch: "
            f"registry={rule.data_contract.stopgap} policy={expected_stopgap}"
        )

    for nudge_key in _collect_registered_nudge_keys():
        key_config = registry.nudge_keys[nudge_key]
        expected_prefix_stopgap = nudge_key.startswith(
            policy.STOPGAP_DISABLED_NUDGE_PREFIXES
        )
        assert key_config.data_contract.stopgap == expected_prefix_stopgap, (
            f"nudge_key '{nudge_key}': stopgap prefix mismatch"
        )


def test_ai_personalizable_matches_analyzer():
    """Registry ai_personalizable flag must agree with engagement_analyzer prefixes."""
    registry = load_nudge_registry()
    for nudge_key in _collect_registered_nudge_keys():
        key_config = registry.nudge_keys[nudge_key]
        expected = nudge_key.startswith(analyzer.AI_PERSONALIZABLE_PREFIXES)
        assert key_config.data_contract.ai_personalizable == expected, (
            f"nudge_key '{nudge_key}': ai_personalizable mismatch: "
            f"registry={key_config.data_contract.ai_personalizable} expected={expected}"
        )


def test_all_dp_sc_116_classes_are_mapped():
    """Every declared DP.SC.116 class must map to a canonical type."""
    registry = load_nudge_registry()
    mapped_classes = set(registry.dp_sc_116_class_map.keys())
    missing = DP_SC_116_CLASSES - mapped_classes
    assert not missing, (
        f"DP.SC.116 class(es) missing from registry mapping: {sorted(missing)}"
    )


def test_dp_sc_116_classes_map_to_valid_canonical_types():
    """DP.SC.116 class values must be known canonical types."""
    registry = load_nudge_registry()
    for class_name, canonical_type in registry.dp_sc_116_class_map.items():
        assert canonical_type in CANONICAL_TYPES, (
            f"DP.SC.116 class '{class_name}' maps to unknown canonical_type "
            f"'{canonical_type}'"
        )


def test_get_helpers_raise_on_unknown_keys():
    registry = load_nudge_registry()
    with _pytest.raises(KeyError):
        get_rule_config("nonexistent_rule", registry=registry)
    with _pytest.raises(KeyError):
        get_nudge_key_config("nonexistent_key", registry=registry)


def test_dp_sc_116_helper_returns_none_for_unknown_class():
    assert get_canonical_type_for_dp_sc_116_class("unknown_class") is None


import pytest as _pytest
