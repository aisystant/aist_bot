"""
Фоновый режим Диагноста — помощник другим ролям (WP-318 Ф9).

Проверяет актуальность cp-профиля и возвращает рекомендацию:
- Навигатор: inline-предложение при онбординг-вопросе и отсутствии/устаревании cp.
- Портной: предложение перед обновлением руководства, если cp старше 90 дней.
- Аттестатор: предложение при bh-переходе без свежего cp-подтверждения.

Не действует напрямую с пилотом без явного разрешения.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from db.queries.cp_assessment import get_latest_cp_assessment

logger = logging.getLogger(__name__)

CP_STALE_DAYS = 90

NAVIGATOR_ONBOARDING_KEYWORDS = (
    "с чего начать",
    "с чего мне начать",
    "куда идти",
    "с нуля",
    "что делать",
    "как начать",
    "первый шаг",
    "как сюда попасть",
    "как устроен",
    "как тут",
    "что здесь",
    "что тут",
)

_DIAGNOSE_CTA = (
    "🔬 Чтобы рекомендации были точными, пройди короткую диагностику "
    "— займёт ~5 минут: /diagnose"
)


async def cp_status(account_id: str) -> dict:
    """Вернуть статус cp-профиля: fresh / stale / missing."""
    latest = await get_latest_cp_assessment(account_id, only_valid=True)
    if not latest:
        return {"state": "missing", "assessment": None}

    assessed_at = latest.get("assessed_at")
    if not assessed_at:
        return {"state": "stale", "assessment": latest}

    try:
        dt = datetime.fromisoformat(assessed_at)
    except (ValueError, TypeError):
        return {"state": "stale", "assessment": latest}

    age_days = (datetime.now(timezone.utc) - dt).days
    if age_days > CP_STALE_DAYS:
        return {"state": "stale", "assessment": latest, "age_days": age_days}
    return {"state": "fresh", "assessment": latest, "age_days": age_days}


async def suggest_for_navigator(account_id: str, question: str) -> Optional[str]:
    """Inline-предложение для Навигатора, если вопрос про вход и нет актуального cp."""
    low = (question or "").lower()
    if not any(kw in low for kw in NAVIGATOR_ONBOARDING_KEYWORDS):
        return None

    status = await cp_status(account_id)
    if status["state"] == "fresh":
        return None
    return _DIAGNOSE_CTA


async def suggest_for_tailor(account_id: str) -> Optional[str]:
    """Предложение для Портного перед обновлением/доставкой руководства."""
    status = await cp_status(account_id)
    if status["state"] != "stale":
        return None

    age = status.get("age_days", "?")
    return (
        f"🔬 Твой диагностический профиль устарел ({age} дней). "
        f"Перед занятием стоит обновить срез — /diagnose (≈5 мин)."
    )


async def suggest_for_attestator(account_id: str, new_bh_stage: int) -> Optional[str]:
    """Предложение для Аттестатора при bh-переходе без свежего cp-подтверждения."""
    status = await cp_status(account_id)
    if status["state"] == "fresh":
        # Двойной gate FORM.089 §5.1 уже пройден.
        return None

    return (
        f"⚠️ Повышение ступени до {new_bh_stage} по bh без свежего "
        f"cp-подтверждения. Предложи пройти /diagnose перед фиксацией перехода."
    )
