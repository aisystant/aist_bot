"""Qualification vs stage: one rule for every screen that shows a person's level.

Stages 1..5 exist only inside the «Ученик» qualification (WP-149 canon, 05.07.2026). From
«Работник» up (LMS numeric level 25, see db/queries/dt_sync.py) a person has a qualification and
no stage, so screens show the qualification instead of the stage (WP-417 Ф8, pilot 13-14.11.2026).

Pure data helpers: no texts, no I/O. Stage names stay with the screens that render them.
"""

import math
from typing import Optional

QUALIFIED_NUMERIC_MIN = 25


def degree_from_profile(profile: Optional[dict]) -> Optional[dict]:
    """Qualification record {level, numeric, ...} from a full twin profile, or None.

    The Profiler's nightly 3_8_degree first, then the LMS value dt_sync writes, which is all there
    is before the first night run after a change (same order as GatewayMCPClient.dt_read_degree).
    """
    if not isinstance(profile, dict):
        return None
    derived = profile.get("_derived") or profile.get("3_derived") or {}
    degree = derived.get("3_8_degree")
    if _has_level(degree):
        return degree
    collected = profile.get("2_collected") or {}
    degree = (collected.get("2_2_courses") or {}).get("qualification_level")
    return degree if _has_level(degree) else None


def qualified_level(degree: Optional[dict]) -> Optional[str]:
    """Qualification name for «Работник» and above; None for «Ученик» and below or unknown."""
    if not _has_level(degree):
        return None
    try:
        numeric = float(degree.get("numeric"))
    except (TypeError, ValueError):
        return None
    if math.isfinite(numeric) and numeric >= QUALIFIED_NUMERIC_MIN:
        return degree["level"]
    return None


def _has_level(degree) -> bool:
    return isinstance(degree, dict) and isinstance(degree.get("level"), str) and bool(degree["level"])
