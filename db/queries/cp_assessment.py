"""
CP-assessment queries — WP-318 Ф6.

# see DP.SC.132, DP.ROLE.042, PD.FORM.089 §6.1 v4.2

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

MANDATORY_SLOTS = ["cp.rhy", "cp.wld", "cp.skl", "cp.iwe", "cp.int", "cp.agt"]
CP_TTL_DAYS = 180


def compute_cp_stage(cp_scores: dict) -> dict:
    """cp_confirmed_stage = min(mandatory). FORM.089 §6.1."""
    vals = {s: int(cp_scores.get(s, 1)) for s in MANDATORY_SLOTS}
    stage = min(vals.values())
    bottleneck = min(vals, key=vals.get)
    return {
        "stage": stage,
        "bottleneck_slot": bottleneck,
        "recommended_stream": f"S{max(1, min(4, stage))}",
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
    rcs_version: str = "v4.2",
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
    logger.info(
        "[cp_assessment] saved id=%s account=%s stage=%s bottleneck=%s",
        row_id, account_id, profile["stage"], profile["bottleneck_slot"],
    )
    return row_id
