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


async def sync_cp_stage_to_onboarding_state(account_id: str, cp_stage: int) -> bool:
    """Мгновенно синхронизировать cp_stage и has_diagnosis в onboarding_state (mini-sync).

    WP-349: вызывается после save_cp_assessment для немедленного отражения ступени.
    Fail-safe — batch-sync исправит на следующем tick, если этот вызов упадёт.
    UPSERT создаёт строку, если onboarding_controller ещё не успел (race-safe).
    """
    try:
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """INSERT INTO learning.onboarding_state (account_id, cp_stage, has_diagnosis)
                   VALUES ($1::uuid, $2, TRUE)
                   ON CONFLICT (account_id) DO UPDATE
                   SET cp_stage = EXCLUDED.cp_stage,
                       has_diagnosis = TRUE""",
                account_id,
                cp_stage,
            )
        tag = result or ""
        return tag.startswith("INSERT") or tag.startswith("UPDATE")
    except Exception as e:
        logger.warning("[WP-349] mini-sync cp_stage failed (non-fatal): %s", e)
        return False


async def write_referral_source(account_id: str, referral_uuid: str) -> bool:
    """Записать referral_source = <ory_uuid рефери> при consent_accept (WP-349 Ф20).

    UPSERT: создаёт строку если onboarding_controller ещё не успел (race-safe).
    Не перезаписывает уже установленный referral_source.
    Возвращает True если строка создана или обновлена.
    """
    try:
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """INSERT INTO learning.onboarding_state (account_id, referral_source)
                   VALUES ($1::uuid, $2)
                   ON CONFLICT (account_id) DO UPDATE
                   SET referral_source = EXCLUDED.referral_source
                   WHERE learning.onboarding_state.referral_source IS NULL""",
                account_id,
                referral_uuid,
            )
        tag = result or ""
        return tag.startswith("INSERT") or (tag.startswith("UPDATE") and tag.split()[-1] == "1")
    except Exception as e:
        logger.warning("[Ф20] write_referral_source failed: %s", e)
        return False


_UPGRADE_MARKER_COLS: dict[str, str] = {
    "f": "msg_f_sent_at",
    "g": "msg_g_sent_at",
}

# Markers whose cooldown is handled by CLASS_CAPPED dedup_key — no DB column needed.
_CAPPED_ONLY_MARKERS: frozenset[str] = frozenset({"onboarding_gap"})


async def write_upgrade_sent_at(account_id: str, marker_key: str) -> bool:
    """Write msg_{f|g}_sent_at = NOW() and last_nudge_at = NOW() atomically.

    Used by scheduler after successful rich-CTA send (WP-349 Ф6/Ф7).
    Markers in _CAPPED_ONLY_MARKERS (e.g. onboarding_gap) rely on CLASS_CAPPED
    dedup_key for cooldown — no DB column update needed; returns True immediately.
    Returns True if row updated (or if marker uses CLASS_CAPPED cooldown).
    """
    if marker_key in _CAPPED_ONLY_MARKERS:
        # H1 fix: CLASS_CAPPED owns per-nudge cooldown, but dual-cooldown check
        # (onboarding_nudged_uuids in scheduler) reads last_nudge_at — update it
        # so onboarding_controller doesn't fire a second nudge the same day.
        return await write_last_nudge_at(account_id)
    col = _UPGRADE_MARKER_COLS.get(marker_key)
    if col is None:
        logger.error("[Setup] write_upgrade_sent_at: unknown marker_key=%s", marker_key)
        return False
    try:
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"""UPDATE learning.onboarding_state
                   SET {col} = NOW(), last_nudge_at = NOW()
                   WHERE account_id = $1::uuid""",
                account_id,
            )
        updated = result.split()[-1] if result else "0"
        return updated == "1"
    except Exception as e:
        logger.warning("[Setup] write_upgrade_sent_at(%s) failed: %s", marker_key, e)
        return False
