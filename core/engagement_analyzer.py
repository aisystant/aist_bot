"""
Engagement Analyzer — threshold rules для автоматических nudges (WP-85, Phase 5C).

Анализирует проекции 2_collected из digital_twins JSONB.
Возвращает список nudge-рекомендаций для каждого пользователя.

Принцип: данные без интерпретации — зеркало. Nudge превращает зеркало в наставника.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# THRESHOLD RULES
# ═══════════════════════════════════════════════════════════

# Each rule: (rule_id, check_fn, cooldown_days)
# check_fn(engagement, user_meta) -> nudge_key | None
# cooldown_days: minimum days between same nudge type for a user

RULES = []


def rule(rule_id, cooldown_days=7):
    """Decorator to register a threshold rule."""
    def decorator(fn):
        RULES.append((rule_id, fn, cooldown_days))
        return fn
    return decorator


@rule("inactivity_3d", cooldown_days=7)
def check_inactivity_3d(engagement, user_meta):
    """Пользователь не взаимодействовал с ботом 3+ дня."""
    last_active = user_meta.get('last_active_date')
    if not last_active:
        return None

    if isinstance(last_active, str):
        try:
            last_active = datetime.fromisoformat(last_active).date()
        except ValueError:
            return None

    days_inactive = (datetime.now(timezone.utc).date() - last_active).days
    if days_inactive >= 3:
        return "nudge_inactivity"
    return None


@rule("streak_drop", cooldown_days=14)
def check_streak_drop(engagement, user_meta):
    """Стрик активности упал (был >= 3, стал 0)."""
    longest = user_meta.get('longest_streak', 0) or 0
    current = user_meta.get('active_days_streak', 0) or 0

    if longest >= 3 and current == 0:
        return "nudge_streak_drop"
    return None


@rule("low_engagement_7d", cooldown_days=14)
def check_low_engagement_7d(engagement, user_meta):
    """Очень низкая активность за 7 дней (< 2 событий)."""
    time_data = engagement.get('2_4_time', {})
    events_7d = time_data.get('events_last_7d', 0) or 0

    if events_7d < 2:
        return "nudge_low_engagement"
    return None


@rule("marathon_stalled", cooldown_days=7)
def check_marathon_stalled(engagement, user_meta):
    """Марафон активен, но нет прогресса за 3+ дня."""
    marathon_status = user_meta.get('marathon_status')
    if marathon_status != 'active':
        return None

    last_active = user_meta.get('last_active_date')
    if not last_active:
        return None

    if isinstance(last_active, str):
        try:
            last_active = datetime.fromisoformat(last_active).date()
        except ValueError:
            return None

    days_inactive = (datetime.now(timezone.utc).date() - last_active).days
    if days_inactive >= 3:
        return "nudge_marathon_stalled"
    return None


@rule("achievement_sessions", cooldown_days=30)
def check_sessions_milestone(engagement, user_meta):
    """Достижение: 10/25/50/100 сессий."""
    account = engagement.get('2_1_account', {})
    sessions = account.get('sessions_total', 0) or 0

    milestones = [100, 50, 25, 10]
    for m in milestones:
        if sessions >= m:
            # Only trigger at threshold (not already notified)
            return f"nudge_sessions_{m}"
    return None


@rule("achievement_active_days", cooldown_days=30)
def check_active_days_milestone(engagement, user_meta):
    """Достижение: 7/14/30 активных дней."""
    time_data = engagement.get('2_4_time', {})
    active_days = time_data.get('active_days', 0) or 0

    milestones = [30, 14, 7]
    for m in milestones:
        if active_days >= m:
            return f"nudge_active_days_{m}"
    return None


# ═══════════════════════════════════════════════════════════
# ANALYZER
# ═══════════════════════════════════════════════════════════

def analyze(engagement: dict, user_meta: dict) -> list[dict]:
    """Проанализировать engagement данные, вернуть список nudge-рекомендаций.

    Args:
        engagement: данные из digital_twins.data['2_collected'] (4 группы)
        user_meta: данные из development.user_state (last_active_date, streak, etc.)

    Returns:
        List of {rule_id, nudge_key, cooldown_days}
    """
    results = []
    for rule_id, check_fn, cooldown_days in RULES:
        try:
            nudge_key = check_fn(engagement or {}, user_meta or {})
            if nudge_key:
                results.append({
                    'rule_id': rule_id,
                    'nudge_key': nudge_key,
                    'cooldown_days': cooldown_days,
                })
        except Exception as e:
            logger.warning(f"[Nudge] Rule {rule_id} failed: {e}")
    return results
