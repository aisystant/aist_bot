"""
Setup Journey DB queries — WP-349 Ф8.

# see DP.SC.155, DP.SC.151

Reader для learning.onboarding_state (first_use_* флаги + cooldown),
writer для last_nudge_at (cooldown-sync при CTA-клике).
cp_assessments читается через cp_assessment.get_latest_cp_assessment().
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from db.connection import get_learning_pool

logger = logging.getLogger(__name__)


async def get_onboarding_state(account_id: str) -> Optional[dict]:
    """Прочитать onboarding_state для пилота. None если строки нет (не opt-in)."""
    try:
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                    first_use_consent,
                    first_use_slot,
                    first_use_points,
                    first_use_connect_browser,
                    first_use_guide_render,
                    first_use_connect_full,
                    has_subscription,
                    has_diagnosis,
                    cp_stage,
                    last_nudge_at,
                    consent_at
                   FROM learning.onboarding_state
                   WHERE account_id = $1::uuid""",
                account_id,
            )
        return dict(row) if row else None
    except Exception as e:
        logger.warning("[Setup] onboarding_state read failed: %s", e)
        return None


async def write_last_nudge_at(account_id: str) -> bool:
    """Записать last_nudge_at = now для cooldown-sync после CTA-клика.

    Возвращает True если строка обновлена, False если не найдена или ошибка.
    """
    try:
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE learning.onboarding_state
                   SET last_nudge_at = NOW()
                   WHERE account_id = $1::uuid""",
                account_id,
            )
        updated = result.split()[-1] if result else "0"
        return updated == "1"
    except Exception as e:
        logger.warning("[Setup] write_last_nudge_at failed: %s", e)
        return False
