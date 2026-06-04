"""
CP-assessment queries — WP-318 Ф6.

# see DP.SC.132, DP.ROLE.042, PD.FORM.089 §6.1 v5.0 (WP-370: MANDATORY_SLOTS=5, cp.iwe→informational, bottleneck=None при stage≥4)

Читает/пишет learning.cp_assessments через learning-pool.
Privacy: только UUID + числа + коды слотов, без raw-текста.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from db.connection import get_learning_pool

logger = logging.getLogger(__name__)

MANDATORY_SLOTS = ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]  # WP-370: align with PD.FORM.089 v5.0 (17 мая 2026) — cp.iwe → informational
CP_TTL_DAYS = 180


def compute_cp_stage(cp_scores: dict) -> dict:
    """cp_confirmed_stage = min(mandatory). FORM.089 §6.1.

    WP-370: bottleneck_slot = None если все mandatory ≥ 4 (нет узких мест на верхних ступенях).
    WP-371: recommended_stream = "РР" для stage=5 (Проактивный → программа «Рабочее развитие»,
            переход к ролям Интеллектуала → Профессионала). Для ст. 1-4 — S1..S4 как раньше.
    """
    vals = {s: int(cp_scores.get(s, 1)) for s in MANDATORY_SLOTS}
    stage = min(vals.values())
    bottleneck = None if stage >= 4 else min(vals, key=vals.get)
    if stage >= 5:
        recommended_stream = "РР"
    else:
        recommended_stream = f"S{max(1, min(4, stage))}"
    return {
        "stage": stage,
        "bottleneck_slot": bottleneck,
        "recommended_stream": recommended_stream,
        "skip_to_stage": stage,
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
