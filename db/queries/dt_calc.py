"""
ТЕХДОЛГ (WP-197): Этот файл принадлежит роли R28 Профилировщик.
Каноническое место: DS-IT-systems/DS-ai-systems/profiler/scripts/dt_calc.py
Копия здесь временная — до реализации механизма импорта между репо (pip install -e / symlink).
Не редактировать здесь — редактировать в profiler/scripts/ и синхронизировать.

Calculation Engine v1.0 — derived indicators из 2_collected + learning_history (WP-151 Ф7a).

Вычисляет IND.3 (derived) из IND.2 (collected) данных.
Источники: engagement + notification_log + coding (WakaTime) + IWE (git/WP) + learning_history (BKT).

Индикаторы:
  IND.3.1.02  slot_regularity        — доля активных дней (→ агентность)
  IND.3.4.01  student_stage           — ступень ученика (0-4, threshold rules)
  IND.3.10.1  integral_agency_index   — агрегированный индекс (0-100)
  IND.3.5.*   mastery_by_area         — BKT P(mastery) по 5 областям из learning_history
  IND.3.6.*   worldview_gaps          — мемы CAT.001 с gap (P(mastery) < порога по ступени)
  IND.3.7.*   mastery_gaps            — практики CAT.002/003 с gap > 0
  IND.3.8.01  qualification_degree    — степень квалификации (из LMS, Методсовет МИМ)
  IND.3.9.01  it_level               — ИТ-уровень (0-3, DigComp-адаптация)
  IND.3.12.01 delivery_style         — рекомендуемый стиль подачи (авто-адаптация)
  IND.3.13.01 notification_responsiveness — отзывчивость на уведомления (0-100)
  IND.3.14.01 learning_autonomy      — учебная автономность (0-100)

v1.0 (WP-151 Ф7a): Production-формулы 5 осей MVP + notification/autonomy.
  Разблокирует WP-149 (Портной), WP-117 (nudge), WP-135 (интерфейс ЦД).

v0.8 (WP-151 Ф5): Полноценный BKT (Bayesian Knowledge Tracing) по мемам CAT.001.
  Вместо MAX depth — вероятностная модель P(mastery) по каждому мему.
  4 параметра: P(L0)=0.1, P(T)=0.3, P(G)=0.25, P(S)=0.1 (литературные значения).
  mastery_by_area = средний P(mastery) по области.
  worldview_gaps использует P(mastery) < порога вместо current_depth < target_depth.

v0.7 (WP-175 Ф5): BKT из learning_history → mastery_by_area, worldview_gaps, mastery_gaps.
  calculate_derived() принимает learning_rows (из development.learning_history).
  При learning_rows=None — возвращает [] для gaps (fallback PD.SPEC.001 §3).

v0.6 (WP-174): Builder Path — альтернативные пороги для Stage 2-4.
  Пользователи с высокой coding/IWE-активностью (T3-T4) могут достичь
  ступени через builder-метрики (2_6_coding, 2_7_iwe) вместо учебных.
  АрхГейт: 62/70 ЭМОГССБ, принцип #5 Evolvability-first.

Пороги: из метамодели DS-MCP/digital-twin-mcp/metamodel/3_derived/.
"""

from __future__ import annotations

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
# BKT — Bayesian Knowledge Tracing (WP-151 Ф5)
# ═══════════════════════════════════════════════════════════

# Литературные значения BKT-параметров (Corbett & Anderson, 1994).
# Калибровка по реальным данным — Ф7 (Лаборатория).
_BKT_P_L0 = 0.1     # начальная вероятность усвоения
_BKT_P_T = 0.3      # вероятность перехода к усвоению после попытки
_BKT_P_G = 0.25     # вероятность угадывания (не знает, но ответил верно)
_BKT_P_S = 0.1      # вероятность промаха (знает, но ошибся)

# Порог P(mastery) для признания мема усвоенным на данной глубине.
# Консервативный: 0.8 (стандартный BKT threshold).
_BKT_MASTERY_THRESHOLD = 0.8


def _bkt_update(p_l: float, correct: bool) -> float:
    """Одно обновление BKT: P(L_n) → P(L_{n+1}).

    Формула (Corbett & Anderson, 1994):
      P(L|correct)  = P(L) * (1 - P(S)) / (P(L) * (1 - P(S)) + (1 - P(L)) * P(G))
      P(L|incorrect) = P(L) * P(S) / (P(L) * P(S) + (1 - P(L)) * (1 - P(G)))
      P(L_next) = P(L|obs) + (1 - P(L|obs)) * P(T)

    Args:
        p_l: текущая P(mastery) [0.0–1.0]
        correct: результат попытки

    Returns:
        обновлённая P(mastery) [0.0–1.0]
    """
    if correct:
        numerator = p_l * (1 - _BKT_P_S)
        denominator = p_l * (1 - _BKT_P_S) + (1 - p_l) * _BKT_P_G
    else:
        numerator = p_l * _BKT_P_S
        denominator = p_l * _BKT_P_S + (1 - p_l) * (1 - _BKT_P_G)

    if denominator == 0:
        p_l_given_obs = p_l
    else:
        p_l_given_obs = numerator / denominator

    # Transition: даже если не усвоил, есть шанс усвоить после попытки
    return p_l_given_obs + (1 - p_l_given_obs) * _BKT_P_T


def _calc_bkt_per_meme(learning_rows: list[dict]) -> dict[str, dict]:
    """Вычислить BKT-состояние для каждого мема из learning_history.

    Группирует попытки по meme_id и depth, прогоняет BKT для каждой пары.
    Возвращает per-meme агрегат: P(mastery) на каждой глубине + общий.

    Args:
        learning_rows: записи из learning_history (element_type='meme'),
            отсортированные по created_at DESC (новые первые).
            Ключи: element_id, element_type, area, depth, passed.

    Returns:
        {meme_id: {
            "area": int,
            "p_mastery": float,         # общая P(mastery) = min по глубинам
            "max_depth_mastered": int,   # макс. глубина с P >= порога
            "attempts": int,            # общее число попыток
            "by_depth": {depth: {"p": float, "attempts": int, "correct": int}},
        }}
    """
    # Собрать попытки по (meme_id, depth) в хронологическом порядке
    # learning_rows приходят DESC — разворачиваем
    meme_attempts: dict[str, list[tuple[int, bool]]] = {}
    meme_areas: dict[str, int] = {}

    for row in reversed(learning_rows):
        if row.get("element_type") != "meme":
            continue
        eid = row.get("element_id")
        if not eid:
            continue
        meme_id = eid.split(".")[-1] if "." in eid else eid
        depth = row.get("depth") or 0
        passed = bool(row.get("passed"))
        area = row.get("area")

        if meme_id not in meme_attempts:
            meme_attempts[meme_id] = []
        meme_attempts[meme_id].append((depth, passed))
        if area:
            meme_areas[meme_id] = area

    result: dict[str, dict] = {}

    for meme_id, attempts in meme_attempts.items():
        # BKT по каждой глубине отдельно
        depth_state: dict[int, dict] = {}

        for depth, passed in attempts:
            if depth not in depth_state:
                depth_state[depth] = {"p": _BKT_P_L0, "attempts": 0, "correct": 0}
            ds = depth_state[depth]
            ds["p"] = _bkt_update(ds["p"], passed)
            ds["attempts"] += 1
            if passed:
                ds["correct"] += 1

        # Общая P(mastery) = min P по всем глубинам ≤ max_depth_attempted
        # (нужно усвоить на ВСЕХ уровнях, не только на одном)
        max_depth_mastered = 0
        for d in sorted(depth_state.keys()):
            if depth_state[d]["p"] >= _BKT_MASTERY_THRESHOLD:
                max_depth_mastered = d

        all_ps = [ds["p"] for ds in depth_state.values()]
        p_mastery = min(all_ps) if all_ps else 0.0
        total_attempts = sum(ds["attempts"] for ds in depth_state.values())

        result[meme_id] = {
            "area": meme_areas.get(meme_id, 0),
            "p_mastery": round(p_mastery, 3),
            "max_depth_mastered": max_depth_mastered,
            "attempts": total_attempts,
            "by_depth": {d: {"p": round(ds["p"], 3), "attempts": ds["attempts"], "correct": ds["correct"]}
                         for d, ds in sorted(depth_state.items())},
        }

    return result


# ═══════════════════════════════════════════════════════════
# IND.3.5 — Mastery by Area (WP-151 Ф5, BKT)
# ═══════════════════════════════════════════════════════════

AREA_KEY_MAP = {1: "knowledge", 2: "tools", 3: "constraints", 4: "environment", 5: "organism"}


def calc_mastery_by_area(learning_rows: list[dict]) -> dict:
    """BKT P(mastery) по каждой из 5 областей из learning_history.

    IND.3.5.*: mastery_by_area[area_key] = средний P(mastery) по мемам области.
    Обратная совместимость: max_depth сохранён для потребителей, которые его используют.

    Args:
        learning_rows: list of dicts с ключами element_id, element_type, area, depth, passed

    Returns:
        {
            "knowledge": float,  # средний P(mastery) [0.0–1.0]
            "tools": float,
            ...
            "max_depth": {"knowledge": int, ...},  # обратная совместимость
            "details": {meme_id: {"p_mastery": float, "attempts": int, ...}, ...}
        }
    """
    bkt = _calc_bkt_per_meme(learning_rows)

    # Средний P(mastery) по области
    area_ps: dict[str, list[float]] = {v: [] for v in AREA_KEY_MAP.values()}
    area_max_depth: dict[str, int] = {v: 0 for v in AREA_KEY_MAP.values()}

    for meme_id, state in bkt.items():
        area = state["area"]
        if area not in AREA_KEY_MAP:
            continue
        key = AREA_KEY_MAP[area]
        area_ps[key].append(state["p_mastery"])
        area_max_depth[key] = max(area_max_depth[key], state["max_depth_mastered"])

    result = {}
    for key in AREA_KEY_MAP.values():
        ps = area_ps[key]
        result[key] = round(sum(ps) / len(ps), 3) if ps else 0.0

    result["max_depth"] = area_max_depth
    result["details"] = {mid: {k: v for k, v in s.items() if k != "by_depth"}
                         for mid, s in bkt.items()}

    return result


# ═══════════════════════════════════════════════════════════
# IND.3.6 — Worldview Gaps (WP-151 Ф5, BKT)
# ═══════════════════════════════════════════════════════════

# Порог P(mastery) по ступени: чем выше ступень, тем строже требование.
_MASTERY_THRESHOLD_BY_STAGE: dict[int, float] = {
    0: 0.6,
    1: 0.65,
    2: 0.7,
    3: 0.8,
    4: 0.9,
}


def calc_worldview_gaps(learning_rows: list[dict], student_stage: int) -> list[dict]:
    """Мемы CAT.001 с P(mastery) ниже порога по ступени (BKT).

    IND.3.6: only мемы, relevant для текущей ступени (entry_stage <= student_stage).
    Использует BKT P(mastery) вместо бинарного current_depth < target_depth.
    Обратная совместимость: current_depth и target_depth сохранены.

    Args:
        learning_rows: записи из learning_history (element_type='meme')
        student_stage: текущая ступень (0-4)

    Returns:
        list[dict] — мемы с gap, отсортированные по area.
        Каждый dict содержит:
          id, area, p_mastery, mastery_threshold, current_depth, target_depth,
          attempts, can_do_passed.
    """
    bkt = _calc_bkt_per_meme(learning_rows)
    threshold = _MASTERY_THRESHOLD_BY_STAGE.get(student_stage, 0.7)
    target_map = _TARGET_DEPTH.get(student_stage, _TARGET_DEPTH[0])
    gaps = []

    for meme_id, meta in _CAT001_META.items():
        area = meta["area"]
        entry_stage = meta["entry_stage"]
        if entry_stage > student_stage:
            continue

        target_depth = target_map.get(area, 1)
        meme_state = bkt.get(meme_id)

        if meme_state:
            p_mastery = meme_state["p_mastery"]
            current_depth = meme_state["max_depth_mastered"]
            attempts = meme_state["attempts"]
            can_do = current_depth > 0
        else:
            p_mastery = 0.0
            current_depth = 0
            attempts = 0
            can_do = False

        # Gap если: P(mastery) ниже порога ИЛИ глубина не достигнута
        if p_mastery < threshold or current_depth < target_depth:
            gaps.append({
                "id": meme_id,
                "area": area,
                "p_mastery": round(p_mastery, 3),
                "mastery_threshold": threshold,
                "current_depth": current_depth,
                "target_depth": target_depth,
                "attempts": attempts,
                "can_do_passed": can_do,
            })

    gaps.sort(key=lambda x: (x["area"], x["p_mastery"]))
    return gaps


# ═══════════════════════════════════════════════════════════
# IND.3.1.02 — Slot Regularity (доля дней со слотом)
# ═══════════════════════════════════════════════════════════

def calc_slot_regularity(collected: dict, as_of: Optional[datetime] = None) -> float:
    """Доля активных дней от общего числа дней с первого события.

    IND.3.1.02: days_with_activity / total_days_since_start.
    Пороги (days/week): Random <1, Practicing ≥3, Systematic ≥5,
                        Disciplined ≥6, Proactive ≥6.7.

    Args:
        collected: данные 2_collected из digital_twins
        as_of: точка отсчёта «сейчас» (UTC). None = datetime.now(timezone.utc).
               Передавай явно при on-demand вызовах чтобы все пользователи
               в одном batch считались на один момент времени.

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

        now = as_of if as_of is not None else datetime.now(timezone.utc)
        total_days = (now - first_dt).days
        if total_days <= 0:
            return 1.0  # Same day

        return min(active_days / total_days, 1.0)
    except (ValueError, TypeError):
        return 0.0


# ═══════════════════════════════════════════════════════════
# IND.3.4.01 — Student Stage (ступень ученика)
# ═══════════════════════════════════════════════════════════

def calc_student_stage(collected: dict, as_of: Optional[datetime] = None) -> dict:
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
    regularity = calc_slot_regularity(collected, as_of=as_of)
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

def calc_integral_agency_index(collected: dict, as_of: Optional[datetime] = None) -> dict:
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
    regularity = calc_slot_regularity(collected, as_of=as_of)
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
# IND.3.8.01 — Qualification Degree (LMS, Методсовет МИМ, WP-151 fix)
# ═══════════════════════════════════════════════════════════

# Степень квалификации (адаптация EQF): уровень системного мышления.
# Отражает глубину работы с предметной областью.
# Source-of-truth: LMS qualification_level_event (Методсовет МИМ).
# dt_sync записывает в 2_collected.2_2_courses.qualification_level при каждом sync.
# При появлении LMS-интеграции — формулу уточнить в Ф7b (Лаборатория).

def calc_qualification_degree(collected: dict, learning_rows: list[dict] | None = None) -> dict:
    """Степень квалификации из LMS (Методсовет МИМ).

    IND.3.8.01: Source-of-truth = LMS qualification_level_event.
    НЕ вычисляется из поведенческих данных — читается из 2_collected.
    dt_sync записывает qualification_level из LMS DB при каждом sync.

    Шкала МИМ: Интересант (L05) → Определяющийся (L08) → Первокурсник (L1) →
    Ученик (L2) → Работник (L25) → Стратег (L3) → Специалист (L4) →
    Практик (L5) → Мастер (L6) → Реформатор (L7) → Деятель (L8).

    Args:
        collected: digital_twins.data['2_collected']
        learning_rows: не используется (оставлен для совместимости сигнатуры)

    Returns:
        {"level": str, "code": str, "numeric": int, "event_date": str|None,
         "reason": str|None, "source": "lms"|"unknown"}
    """
    courses = collected.get('2_2_courses') or {}
    qual = courses.get('qualification_level')

    if qual and isinstance(qual, dict) and qual.get('level'):
        return {
            "level": qual['level'],
            "code": qual.get('code', ''),
            "numeric": qual.get('numeric', 0),
            "event_date": qual.get('event_date'),
            "reason": qual.get('reason'),
            "source": "lms",
        }

    # Нет данных — квалификация не назначена или LMS не подключен
    return {
        "level": "",
        "code": "",
        "numeric": 0,
        "event_date": None,
        "reason": None,
        "source": "unknown",
    }


# ═══════════════════════════════════════════════════════════
# IND.3.9.01 — IT Level (DigComp-адаптация, WP-151 Ф7a)
# ═══════════════════════════════════════════════════════════

def calc_it_level(collected: dict) -> dict:
    """ИТ-уровень (0-3) из поведенческих данных.

    IND.3.9.01: дополняет декларативный IND.1 it_level.
    Если есть данные coding/IWE → вычисляем объективно.
    Иначе → fallback на декларативный (передаётся через 1_declarative, не здесь).

    0 = не может установить ничего сам
    1 = может с подробной инструкцией
    2 = может с поддержкой (полный набор IWE)
    3 = может сам + помогает другим

    Args:
        collected: digital_twins.data['2_collected']

    Returns:
        {"it_level": int, "source": "auto"|"insufficient_data", "evidence": dict}
    """
    coding = collected.get('2_6_coding') or {}
    iwe = collected.get('2_7_iwe') or {}
    time_data = collected.get('2_4_time') or {}

    coding_seconds_30d = coding.get('coding_seconds_30d', 0) or 0
    coding_hours_30d = coding_seconds_30d / 3600
    coding_active_days = coding.get('coding_active_days_30d', 0) or 0
    commits_30d = iwe.get('commits_30d', 0) or 0
    day_opens = iwe.get('day_opens_total', 0) or 0
    ai_chats = time_data.get('ai_chats_total', 0) or 0

    evidence = {
        "coding_hours_30d": round(coding_hours_30d, 1),
        "coding_active_days": coding_active_days,
        "commits_30d": commits_30d,
        "day_opens": day_opens,
        "ai_chats": ai_chats,
    }

    has_coding_data = coding_hours_30d > 0 or commits_30d > 0

    if not has_coding_data:
        return {"it_level": None, "source": "insufficient_data", "evidence": evidence}

    level = 0

    # Level 1: базовое использование (есть AI-чаты или начал coding)
    if ai_chats >= 3 or coding_hours_30d >= 1:
        level = 1

    # Level 2: регулярное использование IWE (coding + commits + day_opens)
    if coding_hours_30d >= 10 and commits_30d >= 5:
        level = 2

    # Level 3: продвинутый (систематический coding + IWE + day_opens)
    if coding_hours_30d >= 40 and commits_30d >= 20 and day_opens >= 5:
        level = 3

    return {"it_level": level, "source": "auto", "evidence": evidence}


# ═══════════════════════════════════════════════════════════
# IND.3.12.01 — Delivery Style Adaptation (WP-151 Ф7a)
# ═══════════════════════════════════════════════════════════

def calc_delivery_style(collected: dict, student_stage: int) -> dict:
    """Рекомендуемый стиль подачи из поведенческих данных + ступени.

    IND.3.12.01: дополняет декларативный IND.1.5 style.
    Авто-адаптация: если поведение указывает на другой стиль, чем заявленный.

    Логика:
    - Ступень 0-1: detailed + examples, 15-20 мин
    - Ступень 2: mixed, 20-30 мин
    - Ступень 3-4: concise + tasks, 30-60 мин

    Коррекция по данным: если пользователь быстро проходит уроки → сократить.
    Если часто просит подробности → расширить.

    Returns:
        {"format": str, "duration_min": int, "complexity": str, "source": "auto"}
    """
    courses = collected.get('2_2_courses') or {}
    practice = collected.get('2_3_practice') or {}
    operations = collected.get('2_8_operations') or {}

    marathon_steps = courses.get('marathon_steps_total', 0) or 0
    marathon_tasks = practice.get('marathon_tasks_total', 0) or 0
    feed_completed = courses.get('feed_completed_total', 0) or 0

    # Коэффициент практичности: доля заданий от уроков
    practice_ratio = marathon_tasks / max(marathon_steps, 1)

    # Базовый стиль по ступени
    if student_stage <= 1:
        fmt = "detailed"
        duration = 20
        complexity = "accessible"
    elif student_stage == 2:
        fmt = "mixed"
        duration = 25
        complexity = "standard"
    else:
        fmt = "concise"
        duration = 30
        complexity = "professional"

    # Коррекция: высокая практичность → больше задач
    if practice_ratio > 0.6 and marathon_tasks >= 5:
        fmt = "tasks-first"

    # Коррекция: много дайджестов → предпочитает краткий формат
    if feed_completed >= 20 and marathon_steps < 5:
        fmt = "digest"
        duration = 15

    return {
        "format": fmt,
        "duration_min": duration,
        "complexity": complexity,
        "source": "auto",
        "evidence": {
            "practice_ratio": round(practice_ratio, 2),
            "marathon_steps": marathon_steps,
            "feed_completed": feed_completed,
        },
    }


# ═══════════════════════════════════════════════════════════
# IND.3.13.01 — Notification Responsiveness (WP-151 Ф7a, WP-117)
# ═══════════════════════════════════════════════════════════

def calc_notification_responsiveness(collected: dict) -> dict:
    """Отзывчивость на уведомления (0-100).

    IND.3.13.01: используется nudge-системой (WP-117) для адаптации
    частоты и типа уведомлений.

    Логика:
    - Доля открытых напоминаний от доставленных
    - Разнообразие типов уведомлений, на которые реагирует
    - Тренд: 7d vs 30d (улучшается или ухудшается)

    Returns:
        {"score": float 0-100, "trend": "improving"|"stable"|"declining", "evidence": dict}
    """
    notifications = collected.get('2_5_notifications') or {}
    operations = collected.get('2_8_operations') or {}

    notif_total = notifications.get('notifications_total', 0) or 0
    notif_7d = notifications.get('notifications_7d', 0) or 0
    notif_30d = notifications.get('notifications_30d', 0) or 0
    notif_types = notifications.get('notification_types', 0) or 0

    reminders_delivered = operations.get('reminders_delivered', 0) or 0
    reminders_opened = operations.get('reminders_opened', 0) or 0

    evidence = {
        "notif_total": notif_total,
        "notif_7d": notif_7d,
        "notif_30d": notif_30d,
        "notif_types": notif_types,
        "reminders_delivered": reminders_delivered,
        "reminders_opened": reminders_opened,
    }

    if notif_total == 0:
        return {"score": 50.0, "trend": "stable", "evidence": evidence}

    # Компонент 1: конверсия напоминаний (50%)
    reminder_rate = reminders_opened / max(reminders_delivered, 1)
    reminder_score = min(reminder_rate * 100, 100)

    # Компонент 2: разнообразие типов (20%)
    # 6+ типов = полный балл
    type_score = min(notif_types / 6 * 100, 100)

    # Компонент 3: интенсивность за последние 30 дней (30%)
    intensity_score = min(notif_30d / 15 * 100, 100)

    score = reminder_score * 0.50 + type_score * 0.20 + intensity_score * 0.30

    # Тренд: сравниваем 7d rate с 30d rate
    if notif_30d > 0:
        weekly_rate = notif_7d / 7
        monthly_rate = notif_30d / 30
        if monthly_rate > 0:
            ratio = weekly_rate / monthly_rate
            if ratio > 1.3:
                trend = "improving"
            elif ratio < 0.7:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "stable"
    else:
        trend = "stable"

    return {"score": round(score, 1), "trend": trend, "evidence": evidence}


# ═══════════════════════════════════════════════════════════
# IND.3.14.01 — Learning Autonomy (WP-151 Ф7a, WP-117)
# ═══════════════════════════════════════════════════════════

def calc_learning_autonomy(collected: dict, student_stage: int) -> dict:
    """Учебная автономность (0-100).

    IND.3.14.01: мера самостоятельности ученика. Используется nudge-системой
    для определения интенсивности подталкивания: высокая автономность → меньше nudge.

    Компоненты:
    - Инициативность: session_start без предварительного напоминания
    - Регулярность: дни/неделю
    - Разнообразие: сколько режимов использует (марафон, лента, тренировки, AI)
    - Ступень: baseline от student_stage

    Returns:
        {"score": float 0-100, "components": dict}
    """
    time_data = collected.get('2_4_time') or {}
    account = collected.get('2_1_account') or {}
    courses = collected.get('2_2_courses') or {}
    practice = collected.get('2_3_practice') or {}
    operations = collected.get('2_8_operations') or {}
    notifications = collected.get('2_5_notifications') or {}

    sessions_total = account.get('sessions_total', 0) or 0
    events_7d = time_data.get('events_last_7d', 0) or 0
    active_days = time_data.get('active_days', 0) or 0
    marathon_steps = courses.get('marathon_steps_total', 0) or 0
    feed_completed = courses.get('feed_completed_total', 0) or 0
    training_passed = practice.get('training_passed_total', 0) or 0
    ai_chats = time_data.get('ai_chats_total', 0) or 0
    reminders_delivered = operations.get('reminders_delivered', 0) or 0

    # 1. Инициативность (30%): сессии без напоминания
    # Аппроксимация: sessions - reminders_delivered = самостоятельные входы
    self_initiated = max(sessions_total - reminders_delivered, 0)
    initiative_ratio = self_initiated / max(sessions_total, 1)
    initiative_score = min(initiative_ratio * 100, 100)

    # 2. Регулярность (25%): дни/неделю нормализовано
    regularity_raw = events_7d / 7
    regularity_score = min(regularity_raw / 0.8 * 100, 100)

    # 3. Разнообразие режимов (20%): сколько разных типов активности
    modes_used = sum([
        1 if marathon_steps > 0 else 0,
        1 if feed_completed > 0 else 0,
        1 if training_passed > 0 else 0,
        1 if ai_chats > 0 else 0,
    ])
    diversity_score = min(modes_used / 3 * 100, 100)  # 3 из 4 = 100%

    # 4. Ступень baseline (25%): Stage прямо отражает автономность
    stage_score = min(student_stage / 4 * 100, 100)

    score = (
        initiative_score * 0.30
        + regularity_score * 0.25
        + diversity_score * 0.20
        + stage_score * 0.25
    )

    return {
        "score": round(score, 1),
        "components": {
            "initiative": round(initiative_score, 1),
            "regularity": round(regularity_score, 1),
            "diversity": round(diversity_score, 1),
            "stage_baseline": round(stage_score, 1),
        },
    }


# ═══════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════

def calculate_derived(collected: dict, learning_rows: list[dict] | None = None, as_of: Optional[datetime] = None) -> dict:
    """Вычислить все derived-индикаторы из 2_collected + learning_history данных.

    Args:
        collected: digital_twins.data['2_collected'] (5+ групп).
            v0.6+: включает 2_6_coding, 2_7_iwe, 2_8_operations.
        learning_rows: список dict из development.learning_history (v0.8+).
            Каждый dict: {element_id, element_type, area, depth, passed, ...}.
            При None — mastery_by_area возвращает нули, gaps — пустые списки (PD.SPEC.001 §3).

    Returns:
        dict для записи в digital_twins.data['3_derived']:
        {
            "3_1_agency": {"slot_regularity": float, ...},
            "3_4_qualification": {"stage": int, "stage_id": str, "path": str, ...},
            "3_5_mastery": {"mastery_by_area": {...}},
            "3_6_worldview": {"worldview_gaps": [...]},
            "3_8_degree": {"level": str, "code": str, "numeric": int, ...},  # v1.1: из LMS
            "3_9_it_level": {"it_level": int|None, ...},    # v1.0: DigComp-адаптация
            "3_10_integral": {"index": float, ...},
            "3_12_delivery_style": {"format": str, ...},    # v1.0: авто-адаптация
            "3_13_notification_resp": {"score": float, ...}, # v1.0: для WP-117
            "3_14_learning_autonomy": {"score": float, ...}, # v1.0: для WP-117
            "calculated_at": ISO timestamp,
            "engine_version": "1.0",
        }
    """
    if not collected:
        return {}

    try:
        now = as_of if as_of is not None else datetime.now(timezone.utc)
        stage_result = calc_student_stage(collected, as_of=now)
        agency_result = calc_integral_agency_index(collected, as_of=now)
        regularity = calc_slot_regularity(collected, as_of=now)
        student_stage = stage_result.get("stage", 0)

        derived = {
            "3_1_agency": {
                "slot_regularity": round(regularity, 3),
                "slot_days_per_week": round(regularity * 7, 1),
            },
            "3_4_qualification": stage_result,
            "3_10_integral": agency_result,
            "calculated_at": now.isoformat(),
            "engine_version": "1.0",
        }

        # ── Ф5: BKT из learning_history (WP-151 Ф5) ─────────────────────────
        if learning_rows is not None:
            mastery = calc_mastery_by_area(learning_rows)
            gaps = calc_worldview_gaps(learning_rows, student_stage)
            derived["3_5_mastery"] = {"mastery_by_area": mastery}
            derived["3_6_worldview"] = {
                "worldview_gaps": gaps,
                "bkt_params": {
                    "p_l0": _BKT_P_L0,
                    "p_t": _BKT_P_T,
                    "p_g": _BKT_P_G,
                    "p_s": _BKT_P_S,
                    "mastery_threshold": _BKT_MASTERY_THRESHOLD,
                },
            }

        # ── Ф7a: 5 осей MVP + notification/autonomy (WP-151 Ф7a) ──────────
        derived["3_8_degree"] = calc_qualification_degree(collected, learning_rows)
        derived["3_9_it_level"] = calc_it_level(collected)
        derived["3_12_delivery_style"] = calc_delivery_style(collected, student_stage)
        derived["3_13_notification_resp"] = calc_notification_responsiveness(collected)
        derived["3_14_learning_autonomy"] = calc_learning_autonomy(collected, student_stage)

        return derived

    except Exception as e:
        logger.error(f"[DT Calc] Error calculating derived: {e}")
        return {}
