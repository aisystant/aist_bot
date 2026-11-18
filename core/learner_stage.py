"""Learner-role stage on profile screens: one order of sources (WP-417 Ф8, pilot 18.11.2026, option 1A).

Screens /twin, /me, «Мои данные» and the twin connection in settings show the stage the same way:

1. the Profiler's nightly stage `3_derived.3_4_qualification.stage`;
2. if it is empty — the stage of the latest self-assessment from /diagnose (`learning.cp_assessments`),
   labelled as a self-assessment with its date. An expired assessment is not shown; one without
   `valid_until` counts only within CP_TTL_DAYS of `assessed_at`, the term new assessments get;
3. otherwise no stage. The legacy profile field `stage` is never shown.

From «Работник» up a person has a qualification and no stage (core/qualification.py), so no stage
and no database read for them.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from core.qualification import qualified_level
from db.queries.cp_assessment import CP_TTL_DAYS, STAGE_NAMES
from i18n import t

logger = logging.getLogger(__name__)

_MOSCOW = ZoneInfo("Europe/Moscow")


def _derived_stage(derived: Optional[dict]):
    return ((derived or {}).get("3_4_qualification") or {}).get("stage")


def learner_stage(derived: Optional[dict], degree: Optional[dict], self_assessment: Optional[dict],
                  now: datetime) -> Optional[dict]:
    """{"stage": int, "assessed_on": date | None} for the screen, or None when there is no stage.

    `derived` is the twin's 3_derived group, `degree` the qualification record
    (core.qualification.degree_from_profile). `assessed_on` is set only when the stage comes from
    the self-assessment (Moscow date).
    """
    if qualified_level(degree):
        return None
    stage = _derived_stage(derived)
    if stage is not None:
        return {"stage": stage, "assessed_on": None}
    return _from_self_assessment(self_assessment, now)


def _from_self_assessment(assessment: Optional[dict], now: datetime) -> Optional[dict]:
    if not assessment or not isinstance(assessment.get("stage"), int):
        return None
    try:
        assessed_at = datetime.fromisoformat(str(assessment["assessed_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    if assessed_at.tzinfo is None:  # TIMESTAMPTZ always has a zone; a bare value is UTC
        assessed_at = assessed_at.replace(tzinfo=timezone.utc)
    if not assessment.get("valid_until") and now - assessed_at > timedelta(days=CP_TTL_DAYS):
        return None
    return {"stage": assessment["stage"], "assessed_on": assessed_at.astimezone(_MOSCOW).date()}


def stage_text(info: dict, lang: str) -> str:
    """«Систематический (3/5)», plus «— самооценка по диагностике от дд.мм.гггг» for a self-assessment."""
    names = STAGE_NAMES.get(lang) or STAGE_NAMES["ru"]
    text = f"{names.get(info['stage'], '?')} ({info['stage']}/5)"
    assessed_on: Optional[date] = info.get("assessed_on")
    if assessed_on is not None:
        text += f" — {t('twin.stage_self_assessed', lang, date=assessed_on.strftime('%d.%m.%Y'))}"
    return text


async def load_self_assessment(derived: Optional[dict], degree: Optional[dict], chat_id: int) -> Optional[dict]:
    """Latest unexpired self-assessment, read only when the screen will use it; None otherwise.

    Best effort: a failed read leaves the screen without a stage instead of breaking it.
    """
    if qualified_level(degree) or _derived_stage(derived) is not None:
        return None
    from db.queries.cp_assessment import get_latest_cp_assessment
    from helpers.dual_write import resolve_ory_id_from_chat

    try:
        account_id = await resolve_ory_id_from_chat(chat_id)
        if not account_id:
            return None
        return await get_latest_cp_assessment(account_id)
    except Exception as e:
        logger.warning("[learner_stage] self-assessment read failed (chat=%s): %r", chat_id, e)
        return None
