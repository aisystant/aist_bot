from __future__ import annotations

"""WP-117 Ф-stopgap: централизованная политика подавления achievement-нуджей.

Этот модуль — единый источник истины для запретного списка achievement-правил
(`sessions_N`, `active_days_N`, `stage_reached_N`), которые в текущей архитектуре
ложно срабатывают повторно из-за устаревших/несогласованных данных цифрового двойника
(эффект Гудхарта: метрика стала целью).

Граница:
- WP-117 (policy) решает, КАКИЕ нуджи подавлять.
- engagement_analyzer.py применяет политику на выходе analyze().
- nudge_producer.py применяет политику как defense-in-depth.
- Очистка notification_queue использует те же критерии для уже поставленных записей.

`agency_high` и `agency_growing` не achievement по своей природе и не попадают
в stopgap без отдельного решения пилота.
"""

import logging
import re

logger = logging.getLogger(__name__)


# Rule IDs, полностью отключённые до починки источников достижений.
STOPGAP_DISABLED_RULES: frozenset[str] = frozenset({
    "achievement_sessions",
    "achievement_active_days",
    "stage_upgrade",
})

# Nudge-key prefixes, отключаемые на всякий случай (defense in depth + защита
# от рассинхронизации rule_id/nudge_key в будущих правилах).
STOPGAP_DISABLED_NUDGE_PREFIXES: tuple[str, ...] = (
    "nudge_sessions_",
    "nudge_active_days_",
    "nudge_stage_reached_",
)


def stopgap_suppression_reason(rule_id: str, nudge_key: str) -> str | None:
    """Вернуть причину подавления WP-117 stopgap, либо None, если нудж разрешён.

    Проверяет оба критерия независимо, чтобы не допустить тихого bypass:
    - rule_id в запретном списке;
    - nudge_key начинается с одного из запретных префиксов.

    Args:
        rule_id: идентификатор правила из engagement_analyzer.
        nudge_key: конкретный ключ нуджа (строка, возможно с суффиксом milestone).

    Returns:
        Строка с причиной, если нудж подавлен; None, если нудж разрешён.
    """
    if rule_id in STOPGAP_DISABLED_RULES:
        return f"wp117_stopgap_rule:{rule_id}"
    if nudge_key.startswith(STOPGAP_DISABLED_NUDGE_PREFIXES):
        return f"wp117_stopgap_prefix:{nudge_key}"
    return None


# PostgreSQL regex для dedup_key в notification_queue.
# dedup_key формируется как f"nudge:{user_id}:{nudge_key}" (nudge_producer.py),
# поэтому суффикс совпадает с nudge_key.
_STOPGAP_DEDUP_KEY_RE = re.compile(
    r"^nudge:[0-9]+:(?:nudge_sessions_[0-9]+|nudge_active_days_[0-9]+|nudge_stage_reached_[0-9]+)$"
)


def stopgap_dedup_key_pattern() -> str:
    """Вернуть PostgreSQL regex для поиска achievement dedup_key в очереди."""
    return _STOPGAP_DEDUP_KEY_RE.pattern


def is_stopgap_dedup_key(dedup_key: str | None) -> bool:
    """Проверить, относится ли dedup_key queued-записи к подавляемому achievement."""
    if not dedup_key:
        return False
    return bool(_STOPGAP_DEDUP_KEY_RE.match(dedup_key))
