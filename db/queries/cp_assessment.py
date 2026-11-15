"""
CP-assessment queries — WP-318 Ф6.

# see DP.SC.132, DP.ROLE.042, PD.FORM.089 §6.1 v5.0 (WP-370: MANDATORY_SLOTS=5, cp.iwe→informational, bottleneck=None при stage≥4)

Читает/пишет learning.cp_assessments через learning-pool.
Privacy: только UUID + числа + коды слотов, без raw-текста.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from db.connection import get_learning_pool

logger = logging.getLogger(__name__)

MANDATORY_SLOTS = ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]  # WP-370: align with PD.FORM.089 v5.0 (17 мая 2026) — cp.iwe → informational
CP_TTL_DAYS = 180

# Stage names by language: single source for /progress, /diagnose and the progress widget.
STAGE_NAMES = {
    "ru": {1: "Случайный", 2: "Практикующий", 3: "Систематический", 4: "Дисциплинированный", 5: "Проактивный"},
    "en": {1: "Casual", 2: "Practicing", 3: "Systematic", 4: "Disciplined", 5: "Proactive"},
    "es": {1: "Ocasional", 2: "Practicante", 3: "Sistemático", 4: "Disciplinado", 5: "Proactivo"},
    "fr": {1: "Occasionnel", 2: "Pratiquant", 3: "Systématique", 4: "Discipliné", 5: "Proactif"},
    "zh": {1: "随机", 2: "实践", 3: "系统", 4: "自律", 5: "主动"},
}

# Человекочитаемые имена слотов (bottleneck_slot). Единственный источник перевода
# cp.* → текст для пилота — используется handlers/diagnose.py и core/onboarder/x3.py,
# не дублировать словарь в UI-коде.
SLOT_NAMES_RU = {
    "cp.rhy": "регулярность и ритм занятий",
    "cp.wld": "мировоззрение и системный взгляд",
    "cp.skl": "умение выбирать и применять способы саморазвития",
    "cp.iwe": "рабочая среда и инструменты",
    "cp.int": "системное мышление",
    "cp.agt": "агентность и инициатива",
}


def describe_bottleneck(bottleneck_slot: str) -> str:
    """Перевести код узкого места (cp.*) в человекочитаемое имя.

    Неизвестный код возвращается как есть (не выдумываем имя).
    """
    return SLOT_NAMES_RU.get(bottleneck_slot, bottleneck_slot)


# Stage of the «Личное развитие» programme a learner fits by their stage (README of the
# practicums, admission table; WP-385 Ф18.2). S0 and S1 both admit stage 1; S0 is
# recommended first. Stage 5 is reached only through the full gate (PD.FORM.089 §5.2),
# never by self-assessment, so it does not appear here as a self-assessment result.
PROGRAM_STAGE_BY_STAGE = {1: "S0", 2: "S1", 3: "S2", 4: "S3", 5: "РР"}

PROGRAM_STAGE_LABELS = {
    "S0": "S0 «Ритмичность» — от расфокуса к результатам",
    "S1": "S1 «Собранность» — от туриста к строителю",
    "S2": "S2 «Агентность» — от пассажира к пилоту",
    "S3": "S3 «Системность» — от тумана к архитектуре",
    "РР": "программа «Рабочее развитие»",
}

# Programme for a qualified person («Работник» and above) by the qualification, not by the
# learner-role diagnostic (PD.FORM.105 §3; pilot 15.11.2026, WP-385 Ф18). Keys are qualification
# names as the twin stores them (db/queries/dt_sync.py); the revolutionary degree has two spellings.
_WORK_DEVELOPMENT = "«Рабочее развитие»"
_ENLIGHTENMENT = "«Просветительская программа» (курса пока нет, путь подберёт наставник)"
QUALIFICATION_PROGRAMMES = {
    "Работник": _WORK_DEVELOPMENT,
    "Стратег": _WORK_DEVELOPMENT,
    "Специалист": _WORK_DEVELOPMENT,
    "Практик": _WORK_DEVELOPMENT,
    "Мастер": "«Исследовательское развитие» или следующий круг «Рабочего развития»",
    "Реформатор": "«Исследовательское развитие» или «Просветительская программа»",
    "Деятель (революционер)": _ENLIGHTENMENT,
    "Революционер": _ENLIGHTENMENT,
}


def qualification_programme(qualification: str) -> str:
    """Programme recommended for a qualification; an unknown name is left to the mentor."""
    return QUALIFICATION_PROGRAMMES.get(qualification, "та, которую подберёт наставник")


def learner_role_has_no_gaps(stage, cp_scores) -> bool:
    """True when the learner-role diagnostic leaves nothing to add: stage 5, or all fives by self-assessment."""
    if isinstance(stage, int) and stage >= 5:
        return True
    return bool(cp_scores) and compute_cp_stage(cp_scores)["stage5_candidate"]

# Highest stage a self-assessment alone may confirm (PD.FORM.089 §5.1 п.5, §6.1:
# skip_to_stage = min(cp_floor, 4)); stage 5 needs the full gate §5.2 (WP-385 Ф18.9).
SELF_ASSESSMENT_STAGE_CAP = 4


def program_stage_for(stage: int) -> str:
    """Programme stage (S0..S3, РР) recommended for a learner stage 1..5."""
    return PROGRAM_STAGE_BY_STAGE[max(1, min(5, int(stage)))]


def program_stage_for_stored_stream(recommended_stream: str) -> str | None:
    """Programme stage for a stored stream code: 'S1'..'S4' (= stage) or legacy 'РР' (= stage 5)."""
    if recommended_stream == "РР":
        return program_stage_for(5)
    if recommended_stream.startswith("S") and recommended_stream[1:].isdigit():
        return program_stage_for(int(recommended_stream[1:]))
    return None


def compute_cp_stage(cp_scores: dict) -> dict:
    """Stage from a self-assessment: minimum of the five mandatory slices, at most 4.

    PD.FORM.089 §6.1 (решение пилота 20.10.2026: cp.agt входит в минимум на всех ступенях).
    All fives give stage 4 with stage5_candidate=True: the transition 4→5 is checked
    by the full gate (§5.2), not by the answers alone (WP-385 Ф18.9).
    bottleneck_slot = 'none' from stage 4 (NOT NULL column; no bottleneck on stages 4-5).
    recommended_stream keeps the stored code S1..S4 (= stage; the learning table may carry
    CHECK recommended_stream IN ('S1'..'S4'), activity-hub migration 013). It is a storage
    code, not a programme stage: screens show program_stage_for(stage) (WP-385 Ф18.2).
    """
    vals = {s: int(cp_scores.get(s, 1)) for s in MANDATORY_SLOTS}
    floor = min(vals.values())
    stage = min(floor, SELF_ASSESSMENT_STAGE_CAP)
    bottleneck = 'none' if stage >= 4 else min(vals, key=vals.get)
    return {
        "stage": stage,
        "bottleneck_slot": bottleneck,
        "recommended_stream": f"S{stage}",
        "skip_to_stage": stage,
        "stage5_candidate": floor >= 5,
    }


async def get_latest_cp_assessment(account_id: str, only_valid: bool = True) -> Optional[dict]:
    """Последний cp-срез; None если нет или TTL истёк."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, stage, bottleneck_slot, recommended_stream,
                      skip_to_stage, cp_scores, assessed_at, valid_until
               FROM learning.cp_assessments
               WHERE account_id = $1::uuid
               ORDER BY assessed_at DESC
               LIMIT 1""",
            account_id,
        )
    if row is None:
        return None
    r = dict(row)
    if only_valid and r.get("valid_until"):
        if r["valid_until"] < datetime.now(timezone.utc):
            return None
    if isinstance(r.get("cp_scores"), str):
        r["cp_scores"] = json.loads(r["cp_scores"])
    r["account_id"] = account_id
    for f in ("assessed_at", "valid_until"):
        if r.get(f):
            r[f] = r[f].isoformat()
    return r


async def save_cp_assessment(
    account_id: str,
    cp_scores: dict,
    source: str,
    interface: str,
    questions_count: Optional[int] = None,
    rcs_version: str = "v5.0",  # WP-370: align with PD.FORM.089 v5.0 (cp.iwe → informational, derive cp.skl)
) -> int:
    """Вычислить профиль и вставить в learning.cp_assessments. Возвращает id."""
    profile = compute_cp_stage(cp_scores)
    valid_until = datetime.now(timezone.utc) + timedelta(days=CP_TTL_DAYS)
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            """INSERT INTO learning.cp_assessments
               (account_id, stage, bottleneck_slot, recommended_stream, skip_to_stage,
                cp_scores, source, interface, questions_count, rcs_version, valid_until)
               VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)
               RETURNING id""",
            account_id,
            profile["stage"],
            profile["bottleneck_slot"],
            profile["recommended_stream"],
            profile["skip_to_stage"],
            json.dumps(cp_scores),
            source,
            interface,
            questions_count,
            rcs_version,
            valid_until,
        )

        # WP-368 Ф6 (R8.1/R8.3): fire-and-forget событие cp_assessment_recorded в
        # КАНОНИЧЕСКУЮ шину public.domain_event (в learning-БД). occurred_at = timestamptz
        # → aware datetime ОК (исключение из правила naive-datetime 10.6, колонка aware).
        # Сбой emit не должен ронять уже сохранённый снимок.
        try:
            occurred_at = datetime.now(timezone.utc)
            await conn.execute(
                """INSERT INTO public.domain_event
                     (source, external_id, event_type, schema_version, payload, account_id, occurred_at)
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6::uuid, $7)
                   ON CONFLICT (source, external_id) DO NOTHING""",
                "cp-assessment",
                f"cp_assessment_recorded-{account_id}-{row_id}",
                "cp_assessment_recorded",
                "v1",
                json.dumps({"source": "cp-assessment", "char_keys": list(cp_scores.keys()),
                            "assessment_id": row_id, "stage": profile["stage"]}),
                account_id,
                occurred_at,
            )
        except Exception as ev_e:
            logger.warning("[cp_assessment] cp_assessment_recorded emit failed (non-fatal): %s", ev_e)

    logger.info(
        "[cp_assessment] saved id=%s account=%s stage=%s bottleneck=%s",
        row_id, account_id, profile["stage"], profile["bottleneck_slot"],
    )
    return row_id
