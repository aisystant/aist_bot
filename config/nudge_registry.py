"""Loader for the WP-117 canonical nudge registry.

The registry lives in `config/nudge_registry.yaml` and is the single source of
truth for the taxonomy of nudge rules, keys and DP.SC.116 classes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from config.settings import BASE_DIR


#: Default path to the canonical nudge registry YAML.
DEFAULT_REGISTRY_PATH = BASE_DIR / "config" / "nudge_registry.yaml"


@dataclass(frozen=True)
class NudgeKeyDataContract:
    """Per-nudge-key data contract derived from the registry."""

    cooldown_days: int
    class_cap: str
    payload_keys: tuple[str, ...]
    ai_personalizable: bool
    stopgap: bool
    dedup_scope: str


@dataclass(frozen=True)
class NudgeKeyConfig:
    """Configuration for one concrete nudge_key."""

    nudge_key: str
    rule_id: str
    canonical_type: str
    eligible_tiers: tuple[str, ...]
    opt_out_category: str
    phase: str
    data_contract: NudgeKeyDataContract


@dataclass(frozen=True)
class RuleConfig:
    """Configuration for one engagement_analyzer rule_id."""

    rule_id: str
    canonical_type: str
    eligible_tiers: tuple[str, ...]
    opt_out_category: str
    phase: str
    nudge_keys: tuple[str, ...]
    data_contract: NudgeKeyDataContract


@dataclass(frozen=True)
class NudgeRegistry:
    """In-memory view of the canonical nudge registry."""

    version: str
    rules: dict[str, RuleConfig]
    nudge_keys: dict[str, NudgeKeyConfig]
    dp_sc_116_class_map: dict[str, str]


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _load_data_contract(raw: dict[str, Any]) -> NudgeKeyDataContract:
    return NudgeKeyDataContract(
        cooldown_days=int(raw.get("cooldown_days", 0)),
        class_cap=str(raw.get("class_cap", "capped")),
        payload_keys=_as_tuple(raw.get("payload_keys")),
        ai_personalizable=bool(raw.get("ai_personalizable", False)),
        stopgap=bool(raw.get("stopgap", False)),
        dedup_scope=str(raw.get("dedup_scope", "recurring")),
    )


def load_nudge_registry(path: Optional[Path] = None) -> NudgeRegistry:
    """Load and normalize the canonical nudge registry.

    Args:
        path: optional override path; defaults to `config/nudge_registry.yaml`.

    Returns:
        NudgeRegistry with indexed rules and nudge_keys.
    """
    path = path or DEFAULT_REGISTRY_PATH
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    version = str(data.get("version", "0.0.0"))
    raw_rules = data.get("rules", [])

    rules: dict[str, RuleConfig] = {}
    nudge_keys: dict[str, NudgeKeyConfig] = {}

    for raw in raw_rules:
        rule_id = str(raw["rule_id"])
        canonical_type = str(raw["canonical_type"])
        eligible_tiers = _as_tuple(raw.get("eligible_tiers"))
        opt_out_category = str(raw.get("opt_out_category", "engagement"))
        phase = str(raw.get("phase", "F1"))
        rule_keys = _as_tuple(raw.get("nudge_keys"))
        data_contract = _load_data_contract(raw.get("data_contract", {}))

        rule = RuleConfig(
            rule_id=rule_id,
            canonical_type=canonical_type,
            eligible_tiers=eligible_tiers,
            opt_out_category=opt_out_category,
            phase=phase,
            nudge_keys=rule_keys,
            data_contract=data_contract,
        )
        if rule_id in rules:
            raise ValueError(f"Duplicate rule_id in nudge registry: {rule_id}")
        rules[rule_id] = rule

        for key in rule_keys:
            if key in nudge_keys:
                raise ValueError(
                    f"Duplicate nudge_key '{key}' mapped to both "
                    f"'{nudge_keys[key].rule_id}' and '{rule_id}'"
                )
            nudge_keys[key] = NudgeKeyConfig(
                nudge_key=key,
                rule_id=rule_id,
                canonical_type=canonical_type,
                eligible_tiers=eligible_tiers,
                opt_out_category=opt_out_category,
                phase=phase,
                data_contract=data_contract,
            )

    dp_sc_116_class_map = {
        str(k): str(v) for k, v in data.get("dp_sc_116_class_map", {}).items()
    }

    return NudgeRegistry(
        version=version,
        rules=rules,
        nudge_keys=nudge_keys,
        dp_sc_116_class_map=dp_sc_116_class_map,
    )


def get_rule_config(rule_id: str, registry: Optional[NudgeRegistry] = None) -> RuleConfig:
    """Return the canonical configuration for a rule_id."""
    registry = registry or _REGISTRY
    if rule_id not in registry.rules:
        raise KeyError(f"rule_id '{rule_id}' is not mapped in the nudge registry")
    return registry.rules[rule_id]


def get_nudge_key_config(
    nudge_key: str, registry: Optional[NudgeRegistry] = None
) -> NudgeKeyConfig:
    """Return the canonical configuration for a nudge_key."""
    registry = registry or _REGISTRY
    if nudge_key not in registry.nudge_keys:
        raise KeyError(f"nudge_key '{nudge_key}' is not mapped in the nudge registry")
    return registry.nudge_keys[nudge_key]


def get_canonical_type_for_dp_sc_116_class(class_name: str) -> Optional[str]:
    """Map a DP.SC.116 class name to its canonical nudge type."""
    return _REGISTRY.dp_sc_116_class_map.get(class_name)


# Module-level singleton, loaded once at import time.
_REGISTRY = load_nudge_registry()
