"""Runtime adapter from the WP-117 canonical registry to WP-418 policy.

WP-117 owns rule identity and recurrence semantics in
``config/nudge_registry.yaml``. WP-418 owns enforcement of cooldown, class cap,
opt-out and channel policy in ``core.nudge_delivery``.
"""

from __future__ import annotations

from config.nudge_registry import load_nudge_registry
from core.nudge_delivery import ClassCap, DedupScope, NudgeTypeConfig


_CLASS_CAPS = {
    "class_capped": ClassCap.CLASS_CAPPED,
    "capped": ClassCap.CLASS_CAPPED,
    "class_any": ClassCap.CLASS_ANY,
    "any": ClassCap.CLASS_ANY,
    "class_exclusive": ClassCap.CLASS_EXCLUSIVE,
    "exclusive": ClassCap.CLASS_EXCLUSIVE,
}


def _build_registered_types() -> dict[str, NudgeTypeConfig]:
    registry = load_nudge_registry()
    registered: dict[str, NudgeTypeConfig] = {}
    for nudge_key, canonical in registry.nudge_keys.items():
        contract = canonical.data_contract
        try:
            class_cap = _CLASS_CAPS[contract.class_cap]
        except KeyError as exc:
            raise ValueError(
                f"Unknown class_cap '{contract.class_cap}' for {nudge_key}"
            ) from exc
        try:
            dedup_scope = DedupScope(contract.dedup_scope)
        except ValueError as exc:
            raise ValueError(
                f"Unknown dedup_scope '{contract.dedup_scope}' for {nudge_key}"
            ) from exc
        registered[nudge_key] = NudgeTypeConfig(
            nudge_type=nudge_key,
            cooldown_days=contract.cooldown_days,
            class_cap=class_cap,
            dedup_scope=dedup_scope,
        )
    return registered


_REGISTERED = _build_registered_types()


def registered_types() -> dict[str, NudgeTypeConfig]:
    """Return a shallow copy of the canonical runtime mapping."""
    return dict(_REGISTERED)


def register_types(
    target_config: dict[str, NudgeTypeConfig] | None = None,
) -> dict[str, NudgeTypeConfig]:
    """Merge canonical WP-117 types into the WP-418 runtime config."""
    if target_config is None:
        from core.nudge_delivery import NUDGE_TYPE_CONFIG

        target_config = NUDGE_TYPE_CONFIG
    target_config.update(_REGISTERED)
    return target_config
