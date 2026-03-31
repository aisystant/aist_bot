"""
ТЕХДОЛГ (WP-197): Этот файл принадлежит роли R28 Профилировщик.
Каноническое место: DS-IT-systems/DS-ai-systems/profiler/scripts/dt_calc.py
Копия здесь временная — до реализации механизма импорта между репо (pip install -e / symlink).
Не редактировать здесь — редактировать в profiler/scripts/ и синхронизировать.

Calculation Engine v0.7 — derived indicators из 2_collected + learning_history (WP-151, WP-174, WP-175 Ф5).

Вычисляет IND.3 (derived) из IND.2 (collected) данных.
Источники: engagement + notification_log + coding (WakaTime) + IWE (git/WP) + learning_history (BKT).

Индикаторы:
  IND.3.1.02  slot_regularity        — доля активных дней (→ агентность)
  IND.3.4.01  student_stage           — ступень ученика (0-4, threshold rules)
  IND.3.10.1  integral_agency_index   — агрегированный индекс (0-100)
  IND.3.5.*   mastery_by_area         — MAX depth по 5 областям из learning_history (WP-175 Ф5)
  IND.3.6.*   worldview_gaps          — мемы CAT.001 с gap > 0 (current_depth < target_depth по ступени)
  IND.3.7.*   mastery_gaps            — практики CAT.002/003 с gap > 0

v0.7 (WP-175 Ф5): BKT из learning_history → mastery_by_area, worldview_gaps, mastery_gaps.
  calculate_derived() принимает learning_rows (из development.learning_history).
  При learning_rows=None — возвращает [] для gaps (fallback PD.SPEC.001 §3).

v0.6 (WP-174): Builder Path — альтернативные пороги для Stage 2-4.
  Пользователи с высокой coding/IWE-активностью (T3-T4) могут достичь
  ступени через builder-метрики (2_6_coding, 2_7_iwe) вместо учебных.
  АрхГейт: 62/70 ЭМОГССБ, принцип #5 Evolvability-first.

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
# Ф5: CAT.001 каталог мемов (BKT-данные для GAP-профиля)
# Источник: DS-principles-curriculum/data/curriculum/CAT.001/
# Формат: {meme_id: {area: int 1-5, entry_stage: int 0-4}}
# ═══════════════════════════════════════════════════════════

_CAT001_META: dict[str, dict] = {
    "M-001": {"area": 1, "entry_stage": 1},
    "M-002": {"area": 1, "entry_stage": 0},
    "M-003": {"area": 1, "entry_stage": 1},
    "M-004": {"area": 1, "entry_stage": 1},
    "M-005": {"area": 1, "entry_stage": 1},
    "M-006": {"area": 1, "entry_stage": 1},
    "M-007": {"area": 1, "entry_stage": 1},
    "M-008": {"area": 1, "entry_stage": 0},
    "M-009": {"area": 1, "entry_stage": 1},
    "M-010": {"area": 1, "entry_stage": 0},
    "M-011": {"area": 1, "entry_stage": 0},
    "M-012": {"area": 1, "entry_stage": 1},
    "M-013": {"area": 2, "entry_stage": 1},
    "M-014": {"area": 2, "entry_stage": 0},
    "M-015": {"area": 2, "entry_stage": 1},
    "M-016": {"area": 2, "entry_stage": 1},
    "M-017": {"area": 2, "entry_stage": 0},
    "M-018": {"area": 2, "entry_stage": 0},
    "M-019": {"area": 2, "entry_stage": 1},
    "M-020": {"area": 3, "entry_stage": 0},
    "M-021": {"area": 3, "entry_stage": 0},
    "M-022": {"area": 3, "entry_stage": 0},
    "M-023": {"area": 3, "entry_stage": 1},
    "M-024": {"area": 3, "entry_stage": 1},
    "M-025": {"area": 3, "entry_stage": 1},
    "M-026": {"area": 3, "entry_stage": 1},
    "M-027": {"area": 3, "entry_stage": 1},
    "M-028": {"area": 3, "entry_stage": 1},
    "M-029": {"area": 3, "entry_stage": 0},
    "M-030": {"area": 3, "entry_stage": 1},
    "M-031": {"area": 3, "entry_stage": 1},
    "M-032": {"area": 4, "entry_stage": 0},
    "M-033": {"area": 4, "entry_stage": 0},
    "M-034": {"area": 4, "entry_stage": 1},
    "M-035": {"area": 4, "entry_stage": 1},
    "M-036": {"area": 4, "entry_stage": 0},
    "M-037": {"area": 4, "entry_stage": 0},
    "M-038": {"area": 4, "entry_stage": 1},
    "M-039": {"area": 4, "entry_stage": 1},
    "M-040": {"area": 5, "entry_stage": 0},
    "M-041": {"area": 5, "entry_stage": 1},
    "M-042": {"area": 5, "entry_stage": 0},
    "M-043": {"area": 5, "entry_stage": 1},
    "M-044": {"area": 5, "entry_stage": 1},
    "M-045": {"area": 5, "entry_stage": 0},
    "M-046": {"area": 3, "entry_stage": 0},
    "M-047": {"area": 3, "entry_stage": 0},
    "M-048": {"area": 3, "entry_stage": 0},
    "M-049": {"area": 4, "entry_stage": 1},
    "M-050": {"area": 4, "entry_stage": 1},
    "M-051": {"area": 1, "entry_stage": 1},
    "M-052": {"area": 1, "entry_stage": 1},
    "M-053": {"area": 5, "entry_stage": 1},
    "M-054": {"area": 5, "entry_stage": 1},
    "M-055": {"area": 5, "entry_stage": 1},
    "M-056": {"area": 5, "entry_stage": 0},
    "M-057": {"area": 5, "entry_stage": 0},
    "M-058": {"area": 5, "entry_stage": 0},
    "M-059": {"area": 5, "entry_stage": 0},
    "M-060": {"area": 5, "entry_stage": 1},
    "M-061": {"area": 5, "entry_stage": 0},
    "M-062": {"area": 5, "entry_stage": 0},
    "M-063": {"area": 5, "entry_stage": 0},
    "M-064": {"area": 5, "entry_stage": 1},
}

# Нормативная целевая глубина мемов по ступени и области (PD.FORM.080 §9).
# target_depth[student_stage][area] = int 1-3 (макс. глубина мема по фазе)
# Ступень 0 (Случайный): цель depth=1 только по ведущим осям (1=Знания, 5=Организм)
# Ступень 1→2 (Практикующий): ведущие оси 2=Инструменты, 3=Ограничения → depth=1 все
# Ступень 2→3 (Систематический): depth=2 для Знания(1), Ограничения(3), Окружение(4)
# Ступень 3→4 (Дисциплинированный): depth=3 для Знания(1), Окружение(4)
# Ступень 4 (Проактивный): depth=3 для всех
_TARGET_DEPTH: dict[int, dict[int, int]] = {
    0: {1: 1, 2: 1, 3: 1, 4: 1, 5: 1},
    1: {1: 1, 2: 1, 3: 1, 4: 1, 5: 1},
    2: {1: 2, 2: 1, 3: 2, 4: 2, 5: 1},
    3: {1: 3, 2: 2, 3: 2, 4: 3, 5: 2},
    4: {1: 3, 2: 3, 3: 3, 4: 3, 5: 3},
}


# ═══════════════════════════════════════════════════════════
# IND.3.5 — Mastery by Area (WP-175 Ф5)
# ═══════════════════════════════════════════════════════════

def calc_mastery_by_area(learning_rows: list[dict]) -> dict[str, int]:
    """MAX depth по каждой из 5 областей из learning_history (schema_version=2).

    IND.3.5.*: mastery_by_area[area_key] = max depth where passed=True.
    Только записи с passed=True и element_type='meme' (worldview).

    Args:
        learning_rows: list of dicts с ключами area (int 1-5), depth (int), passed (bool)

    Returns:
        {"knowledge": int, "tools": int, "constraints": int, "environment": int, "organism": int}
        Значения 0–3. 0 = не начата область.
    """
    area_key_map = {1: "knowledge", 2: "tools", 3: "constraints", 4: "environment", 5: "organism"}
    result = {v: 0 for v in area_key_map.values()}

    for row in learning_rows:
        area = row.get("area")
        depth = row.get("depth")
        passed = row.get("passed")
        if area not in area_key_map or not depth or not passed:
            continue
        key = area_key_map[area]
        result[key] = max(result[key], int(depth))

    return result


# ═══════════════════════════════════════════════════════════
# IND.3.6 — Worldview Gaps (WP-175 Ф5)
# ═══════════════════════════════════════════════════════════

def calc_worldview_gaps(learning_rows: list[dict], student_stage: int) -> list[dict]:
    """Мемы CAT.001 с gap > 0 (current_depth < target_depth по ступени).

    IND.3.6: only мемы, relevant для текущей ступени (entry_stage <= student_stage).
    Текущая глубина = MAX depth где passed=True для данного meme_id.
    can_do_passed = есть ли хотя бы одна запись passed=True для этого мема.

    Args:
        learning_rows: записи из learning_history (element_type='meme')
        student_stage: текущая ступень (0-4)

    Returns:
        list[dict] — только мемы с gap > 0, отсортированные по area.
        Пустой список если нет gap или нет данных.
    """
    # Собрать max_depth и can_do по meme_id из истории
    meme_depth: dict[str, int] = {}
    meme_can_do: dict[str, bool] = {}

    for row in learning_rows:
        if row.get("element_type") != "meme":
            continue
        eid = row.get("element_id")
        if not eid:
            continue
        # Нормализация: "CAT.001.M-001" → "M-001"
        meme_id = eid.split(".")[-1] if "." in eid else eid
        depth = row.get("depth") or 0
        passed = bool(row.get("passed"))

        if passed:
            meme_depth[meme_id] = max(meme_depth.get(meme_id, 0), depth)
            meme_can_do[meme_id] = True
        elif meme_id not in meme_can_do:
            meme_can_do[meme_id] = False

    target_map = _TARGET_DEPTH.get(student_stage, _TARGET_DEPTH[0])
    gaps = []

    for meme_id, meta in _CAT001_META.items():
        area = meta["area"]
        entry_stage = meta["entry_stage"]
        if entry_stage > student_stage:
            continue  # мем ещё не релевантен

        target_depth = target_map.get(area, 1)
        current_depth = meme_depth.get(meme_id, 0)

        if current_depth < target_depth:
            gaps.append({
                "id": meme_id,
                "area": area,
                "current_depth": current_depth,
                "target_depth": target_depth,
                "can_do_passed": meme_can_do.get(meme_id, False),
            })

    gaps.sort(key=lambda x: x["area"])
    return gaps


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
    Два пути определения ступени (v0.6, WP-174):
      - Учебный путь: engagement + training + marathon (v0.5)
      - Builder путь: coding (WakaTime) + IWE (git, WP, sessions)

    Алгоритм: bottom-up проверка порогов. Каждая ступень требует
    выполнения ВСЕХ условий учебного ИЛИ builder пути.
    Наивысшая удовлетворённая ступень = результат.

    Builder-пороги обоснование (калибровка по DP.SC.020, DP.ASSIST.001):
      Stage 2: «ежедневный слот ≥4 недель, есть система» →
               coding ≥40h/мес + ≥15 активных дней + ≥4 дня/нед
      Stage 3: «≥10 часов/неделю, 3+ месяцев» →
               coding ≥80h/мес + ≥50 коммитов + ≥5.5 дней/нед
      Stage 4: «сам инициирует развитие» →
               coding ≥120h/мес + ≥100 коммитов + ≥3 завершённых РП

    Returns:
        {
            "stage": int (0-4),
            "stage_id": "STG.Student.Random",
            "stage_name_ru": "Случайный",
            "path": "learner" | "builder",  # какой путь определил ступень
            "evidence": {...},
        }
    """
    time_data = collected.get('2_4_time') or {}
    account = collected.get('2_1_account') or {}
    courses = collected.get('2_2_courses') or {}
    practice = collected.get('2_3_practice') or {}
    notifications = collected.get('2_5_notifications') or {}

    # ─── Builder path data (v0.6, WP-174) ───
    coding = collected.get('2_6_coding') or {}
    iwe = collected.get('2_7_iwe') or {}

    # Extract learner metrics
    active_days = time_data.get('active_days', 0) or 0
    events_7d = time_data.get('events_last_7d', 0) or 0
    events_30d = time_data.get('events_last_30d', 0) or 0
    sessions_total = account.get('sessions_total', 0) or 0
    marathon_steps = courses.get('marathon_steps_total', 0) or 0
    feed_completed = courses.get('feed_completed_total', 0) or 0
    training_passed = practice.get('training_passed_total', 0) or 0
    notifications_30d = notifications.get('notifications_30d', 0) or 0

    # Extract builder metrics
    coding_seconds_30d = coding.get('coding_seconds_30d', 0) or 0
    coding_hours_30d = coding_seconds_30d / 3600
    coding_active_days = coding.get('coding_active_days_30d', 0) or 0
    commits_30d = iwe.get('commits_30d', 0) or 0
    wp_completed = iwe.get('wp_completed_total', 0) or 0

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
        "coding_hours_30d": round(coding_hours_30d, 1),
        "coding_active_days": coding_active_days,
        "commits_30d": commits_30d,
        "wp_completed": wp_completed,
    }

    # Bottom-up: check from lowest to highest stage
    stage = STAGE_RANDOM
    path = "learner"

    # Stage 1 (Practicing): ≥3 sessions, ≥2 events/7d, regularity ≥2 days/week
    # Builder path не нужен — порог и так не требует training
    if sessions_total >= 3 and events_7d >= 2 and days_per_week >= 2:
        stage = STAGE_PRACTICING

    # Stage 2 (Systematic):
    #   Learner: ≥10 sessions, ≥5 events/7d, ≥4 days/week, training ≥3
    #   Builder: coding ≥40h/мес, ≥15 coding days, ≥4 days/week
    learner_s2 = (sessions_total >= 10 and events_7d >= 5
                  and days_per_week >= 4 and training_passed >= 3)
    builder_s2 = (coding_hours_30d >= 40 and coding_active_days >= 15
                  and days_per_week >= 4)
    if learner_s2 or builder_s2:
        stage = STAGE_SYSTEMATIC
        if builder_s2 and not learner_s2:
            path = "builder"

    # Stage 3 (Disciplined):
    #   Learner: ≥30 sessions, ≥10 events/7d, ≥5.5 days/week, training ≥10, marathon ≥5
    #   Builder: coding ≥80h/мес, ≥50 commits/мес, ≥5.5 days/week
    learner_s3 = (sessions_total >= 30 and events_7d >= 10
                  and days_per_week >= 5.5
                  and training_passed >= 10 and marathon_steps >= 5)
    builder_s3 = (coding_hours_30d >= 80 and commits_30d >= 50
                  and days_per_week >= 5.5)
    if learner_s3 or builder_s3:
        stage = STAGE_DISCIPLINED
        if builder_s3 and not learner_s3:
            path = "builder"

    # Stage 4 (Proactive):
    #   Learner: ≥50 sessions, ≥6 days/week, ≥60 events/30d, training ≥20
    #   Builder: coding ≥120h/мес, ≥100 commits/мес, ≥3 completed WPs
    learner_s4 = (sessions_total >= 50 and days_per_week >= 6
                  and events_30d >= 60 and training_passed >= 20)
    builder_s4 = (coding_hours_30d >= 120 and commits_30d >= 100
                  and wp_completed >= 3)
    if learner_s4 or builder_s4:
        stage = STAGE_PROACTIVE
        if builder_s4 and not learner_s4:
            path = "builder"

    return {
        "stage": stage,
        "stage_id": STAGE_NAMES[stage],
        "stage_name_ru": STAGE_NAMES_RU[stage],
        "path": path,
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

def calculate_derived(collected: dict, learning_rows: list[dict] | None = None) -> dict:
    """Вычислить все derived-индикаторы из 2_collected + learning_history данных.

    Args:
        collected: digital_twins.data['2_collected'] (5+ групп).
            v0.6: включает 2_6_coding и 2_7_iwe для builder path.
        learning_rows: список dict из development.learning_history (v0.7, WP-175 Ф5).
            Каждый dict: {element_id, element_type, area, depth, passed, ...}.
            При None — mastery_by_area возвращает нули, gaps — пустые списки (PD.SPEC.001 §3).

    Returns:
        dict для записи в digital_twins.data['3_derived']:
        {
            "3_1_agency": {"slot_regularity": float, ...},
            "3_4_qualification": {"stage": int, "stage_id": str, "path": str, ...},
            "3_5_mastery": {"mastery_by_area": {...}},           # WP-175 Ф5
            "3_6_worldview": {"worldview_gaps": [...]},          # WP-175 Ф5
            "3_10_integral": {"index": float, "components": {...}},
            "calculated_at": ISO timestamp,
            "engine_version": "0.7",
        }
    """
    if not collected:
        return {}

    try:
        stage_result = calc_student_stage(collected)
        agency_result = calc_integral_agency_index(collected)
        regularity = calc_slot_regularity(collected)

        derived = {
            "3_1_agency": {
                "slot_regularity": round(regularity, 3),
                "slot_days_per_week": round(regularity * 7, 1),
            },
            "3_4_qualification": stage_result,
            "3_10_integral": agency_result,
            "calculated_at": datetime.now(timezone.utc).isoformat(),
            "engine_version": "0.7",
        }

        # ── Ф5: BKT из learning_history ──────────────────────────────────────
        if learning_rows is not None:
            student_stage = stage_result.get("stage", 0)
            mastery = calc_mastery_by_area(learning_rows)
            gaps = calc_worldview_gaps(learning_rows, student_stage)
            derived["3_5_mastery"] = {"mastery_by_area": mastery}
            derived["3_6_worldview"] = {"worldview_gaps": gaps}

        return derived

    except Exception as e:
        logger.error(f"[DT Calc] Error calculating derived: {e}")
        return {}
