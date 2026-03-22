"""
Calculation Engine v0.5 — derived indicators из 2_collected (WP-151 Ф4).

Вычисляет IND.3 (derived) из IND.2 (collected) данных бота.
Без LMS/Club/BKT — только engagement + notification_log.

Индикаторы:
  IND.3.1.02  slot_regularity        — доля активных дней (→ агентность)
  IND.3.4.01  student_stage           — ступень ученика (0-4, threshold rules)
  IND.3.10.1  integral_agency_index   — агрегированный индекс (0-100)

Расширение: LMS/Club данные, BKT (Ф5), мемы (Ф1).
Пороги: из метамодели DS-MCP/digital-twin-mcp/metamodel/3_derived/.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# STUDENT STAGES (PD.FORM.003)
# ═══════════════════════════════════════════════════════════

STAGE_RANDOM = 0        # Случайный
STAGE_PRACTICING = 1    # Практикующий
STAGE_SYSTEMATIC = 2    # Систематический
STAGE_DISCIPLINED = 3   # Дисциплинированный
STAGE_PROACTIVE = 4     # Проактивный

STAGE_NAMES = {
    0: "STG.Student.Random",
    1: "STG.Student.Practicing",
    2: "STG.Student.Systematic",
    3: "STG.Student.Disciplined",
    4: "STG.Student.Proactive",
}

STAGE_NAMES_RU = {
    0: "Случайный",
    1: "Практикующий",
    2: "Систематический",
    3: "Дисциплинированный",
    4: "Проактивный",
}


# ═══════════════════════════════════════════════════════════
# IND.3.1.02 — Slot Regularity (доля дней со слотом)
# ═══════════════════════════════════════════════════════════

def calc_slot_regularity(collected: dict) -> float:
    """Доля активных дней от общего числа дней с первого события.

    IND.3.1.02: days_with_activity / total_days_since_start.
    Пороги (days/week): Random <1, Practicing ≥3, Systematic ≥5,
                        Disciplined ≥6, Proactive ≥6.7.

    Returns:
        float 0.0–1.0 (ratio) or 0.0 if insufficient data.
    """
    time_data = collected.get('2_4_time') or {}
    account = collected.get('2_1_account') or {}

    active_days = time_data.get('active_days', 0) or 0
    first_event = account.get('first_event_at')

    if not first_event or active_days == 0:
        return 0.0

    try:
        if isinstance(first_event, str):
            # Handle both ISO formats
            first_dt = datetime.fromisoformat(first_event.replace('Z', '+00:00'))
        else:
            first_dt = first_event

        if first_dt.tzinfo is None:
            first_dt = first_dt.replace(tzinfo=timezone.utc)

        total_days = (datetime.now(timezone.utc) - first_dt).days
        if total_days <= 0:
            return 1.0  # Same day

        return min(active_days / total_days, 1.0)
    except (ValueError, TypeError):
        return 0.0


# ═══════════════════════════════════════════════════════════
# IND.3.4.01 — Student Stage (ступень ученика)
# ═══════════════════════════════════════════════════════════

def calc_student_stage(collected: dict) -> dict:
    """Определить ступень ученика по threshold rules из метамодели.

    IND.3.4.01: categorical enum STG.Student.*.
    MVP-версия (без LMS): использует engagement + notifications.

    Алгоритм: bottom-up проверка порогов. Каждая ступень требует
    выполнения ВСЕХ условий. Наивысшая удовлетворённая ступень = результат.

    Returns:
        {
            "stage": int (0-4),
            "stage_id": "STG.Student.Random",
            "stage_name_ru": "Случайный",
            "evidence": {...},  # метрики, по которым определено
        }
    """
    time_data = collected.get('2_4_time') or {}
    account = collected.get('2_1_account') or {}
    courses = collected.get('2_2_courses') or {}
    practice = collected.get('2_3_practice') or {}
    notifications = collected.get('2_5_notifications') or {}

    # Extract metrics
    active_days = time_data.get('active_days', 0) or 0
    events_7d = time_data.get('events_last_7d', 0) or 0
    events_30d = time_data.get('events_last_30d', 0) or 0
    sessions_total = account.get('sessions_total', 0) or 0
    marathon_steps = courses.get('marathon_steps_total', 0) or 0
    feed_completed = courses.get('feed_completed_total', 0) or 0
    training_passed = practice.get('training_passed_total', 0) or 0
    notifications_30d = notifications.get('notifications_30d', 0) or 0

    # Slot regularity for stage determination
    regularity = calc_slot_regularity(collected)
    days_per_week = regularity * 7

    # Evidence dict
    evidence = {
        "active_days": active_days,
        "events_7d": events_7d,
        "events_30d": events_30d,
        "days_per_week": round(days_per_week, 1),
        "sessions_total": sessions_total,
        "marathon_steps": marathon_steps,
        "training_passed": training_passed,
        "regularity": round(regularity, 3),
    }

    # Bottom-up: check highest stage first
    stage = STAGE_RANDOM

    # Stage 1 (Practicing): ≥3 sessions, ≥2 events/7d, regularity ≥3 days/week
    if sessions_total >= 3 and events_7d >= 2 and days_per_week >= 2:
        stage = STAGE_PRACTICING

    # Stage 2 (Systematic): ≥10 sessions, ≥5 events/7d,
    #   regularity ≥5 days/week, some training
    if (sessions_total >= 10 and events_7d >= 5
            and days_per_week >= 4 and training_passed >= 3):
        stage = STAGE_SYSTEMATIC

    # Stage 3 (Disciplined): ≥30 sessions, ≥10 events/7d,
    #   regularity ≥6 days/week, active learning
    if (sessions_total >= 30 and events_7d >= 10
            and days_per_week >= 5.5
            and training_passed >= 10 and marathon_steps >= 5):
        stage = STAGE_DISCIPLINED

    # Stage 4 (Proactive): ≥50 sessions, regularity ≥6.7 days/week,
    #   high engagement, community contribution (not measurable yet)
    if (sessions_total >= 50 and days_per_week >= 6
            and events_30d >= 60 and training_passed >= 20):
        stage = STAGE_PROACTIVE

    return {
        "stage": stage,
        "stage_id": STAGE_NAMES[stage],
        "stage_name_ru": STAGE_NAMES_RU[stage],
        "evidence": evidence,
    }


# ═══════════════════════════════════════════════════════════
# IND.3.10.1 — Integral Agency Index (0–100)
# ═══════════════════════════════════════════════════════════

def calc_integral_agency_index(collected: dict) -> dict:
    """Агрегированный индекс агентности из групп 2_1–2_5.

    IND.3.10.1: weighted sum of normalized metrics → 0-100 scale.

    Компоненты (веса):
      - Регулярность (slot_regularity):       30%
      - Активность (events intensity):        25%
      - Обучение (courses + practice):         25%
      - Реакция на уведомления (notifications): 10%
      - Стаж (account longevity):              10%

    Returns:
        {
            "index": float (0-100),
            "components": {...},  # breakdowns
        }
    """
    time_data = collected.get('2_4_time') or {}
    account = collected.get('2_1_account') or {}
    courses = collected.get('2_2_courses') or {}
    practice = collected.get('2_3_practice') or {}
    notifications = collected.get('2_5_notifications') or {}

    # 1. Regularity (30%) — slot_regularity normalized to 0-100
    regularity = calc_slot_regularity(collected)
    regularity_score = min(regularity * 100 / 0.8, 100)  # 80%+ = 100

    # 2. Activity intensity (25%) — events_30d normalized
    events_30d = time_data.get('events_last_30d', 0) or 0
    # 60+ events/30d = full score (2/day)
    activity_score = min(events_30d / 60 * 100, 100)

    # 3. Learning (25%) — combo of marathon + feed + training
    marathon_steps = courses.get('marathon_steps_total', 0) or 0
    feed_completed = courses.get('feed_completed_total', 0) or 0
    training_passed = practice.get('training_passed_total', 0) or 0
    # Normalized: 20 steps + 10 feed + 10 training = full score
    learning_raw = (
        min(marathon_steps / 20, 1) * 40
        + min(feed_completed / 10, 1) * 30
        + min(training_passed / 10, 1) * 30
    )
    learning_score = min(learning_raw, 100)

    # 4. Notification responsiveness (10%)
    notif_total = notifications.get('notifications_total', 0) or 0
    notif_30d = notifications.get('notifications_30d', 0) or 0
    # Having notifications means the system is active; 10+ notifications/30d = engaged
    notif_score = min(notif_30d / 10 * 100, 100) if notif_total > 0 else 50

    # 5. Longevity (10%) — active_days normalized
    active_days = time_data.get('active_days', 0) or 0
    # 30+ active days = full score
    longevity_score = min(active_days / 30 * 100, 100)

    # Weighted sum
    index = (
        regularity_score * 0.30
        + activity_score * 0.25
        + learning_score * 0.25
        + notif_score * 0.10
        + longevity_score * 0.10
    )

    return {
        "index": round(index, 1),
        "components": {
            "regularity": round(regularity_score, 1),
            "activity": round(activity_score, 1),
            "learning": round(learning_score, 1),
            "notifications": round(notif_score, 1),
            "longevity": round(longevity_score, 1),
        },
    }


# ═══════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════

def calculate_derived(collected: dict) -> dict:
    """Вычислить все derived-индикаторы из 2_collected данных.

    Args:
        collected: digital_twins.data['2_collected'] (5+ групп)

    Returns:
        dict для записи в digital_twins.data['3_derived']:
        {
            "3_1_agency": {"slot_regularity": float, ...},
            "3_4_qualification": {"stage": int, "stage_id": str, ...},
            "3_10_integral": {"index": float, "components": {...}},
            "calculated_at": ISO timestamp,
            "engine_version": "0.5",
        }
    """
    if not collected:
        return {}

    try:
        stage_result = calc_student_stage(collected)
        agency_result = calc_integral_agency_index(collected)
        regularity = calc_slot_regularity(collected)

        return {
            "3_1_agency": {
                "slot_regularity": round(regularity, 3),
                "slot_days_per_week": round(regularity * 7, 1),
            },
            "3_4_qualification": stage_result,
            "3_10_integral": agency_result,
            "calculated_at": datetime.now(timezone.utc).isoformat(),
            "engine_version": "0.5",
        }
    except Exception as e:
        logger.error(f"[DT Calc] Error calculating derived: {e}")
        return {}
