from __future__ import annotations

"""WP-117 Ф-cross-pipeline-contradiction: кросс-конвейерный арбитр (S3).

Увеличение 1 (деплой 03.10.2026) перевело check_low_engagement_7d на живой
источник — конкретный инцидент (похвала day_14 vs упрёк о низкой активности
той же недели) закрыт. Этот модуль — defense-in-depth на случай ДРУГОГО
правила: читает оба журнала (milestone C3 в lead-пуле, нудж-доставка в
learning-пуле) и не даёт уйти упрёку, если недавно была похвала, А правило
упрёка не из LIVE_SOURCE_RULES (т.е. само не является самым свежим
наблюдением — см. activity_sources.py).
"""

import logging
from datetime import datetime, timezone
from typing import Callable

from config.nudge_registry import get_nudge_key_config
from core.activity_sources import LIVE_SOURCE_RULES
from db.queries.conversion import fetch_recent_milestone_claims
from db.queries.notifications import fetch_recent_nudge_sends

logger = logging.getLogger(__name__)


def _as_aware_utc(value: datetime) -> datetime:
    """Нормализовать naive/aware datetime к aware UTC.

    conversion_event.shown_at и domain_event.ingested_at — две разные таблицы
    в двух разных Neon-БД; нет гарантии, что обе хранят один и тот же тип
    (TIMESTAMP naive vs TIMESTAMPTZ aware). Сравнение/сортировка naive против
    aware роняет TypeError — тот же класс бага, что уже ловили на МСК/UTC
    (CLAUDE.md §10.6, §Anti-hallucination). Наивный datetime здесь считается
    UTC (конвенция этой БД, см. CLAUDE.md §10.6 бота).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _milestone_polarity(action: str | None) -> str | None:
    """'shown:active' → 'active'; legacy 'shown' (без полярности) → None."""
    if not action or ":" not in action:
        return None
    polarity = action.split(":", 1)[1].strip().lower()
    return polarity if polarity in ("active", "inactive") else None


async def get_recent_claims(chat_id: int, hours: int = 72) -> list[dict]:
    """Недавние «утверждения» об активности пользователя из обоих конвейеров.

    Returns:
        [{"source": "milestone"|"nudge", "claim": "high_activity"|"low_activity",
          "observed_at": aware-UTC datetime}, ...], новые первыми.
    """
    claims: list[dict] = []

    for row in await fetch_recent_milestone_claims(chat_id, hours):
        polarity = _milestone_polarity(row["action"])
        if polarity == "active":
            claims.append({
                "source": "milestone", "claim": "high_activity",
                "observed_at": _as_aware_utc(row["observed_at"]),
            })
        elif polarity == "inactive":
            claims.append({
                "source": "milestone", "claim": "low_activity",
                "observed_at": _as_aware_utc(row["observed_at"]),
            })

    for row in await fetch_recent_nudge_sends(chat_id, hours):
        try:
            canonical = get_nudge_key_config(row["nudge_key"]).canonical_type
        except KeyError:
            # Незамапленный nudge_key (тот же fail-open дефолт, что и у
            # canonical_type_for_rule в nudge_producer.py) — не роняем арбитраж
            # ради одного пользователя, но видимость сигнала нужна: молчание
            # здесь означает, что НОВЫЙ recognition-нудж не защитит никого
            # от S3, пока его не впишут в реестр.
            logger.warning(
                "[Claims] Unmapped nudge_key %s for chat_id=%s — skipped in cross-pipeline arbitration",
                row["nudge_key"], chat_id,
            )
            continue
        if canonical == "recognition_progress":
            claim = "high_activity"
        elif canonical == "engagement_reactivation":
            claim = "low_activity"
        else:
            continue
        claims.append({
            "source": "nudge", "claim": claim,
            "observed_at": _as_aware_utc(row["observed_at"]),
        })

    claims.sort(key=lambda c: c["observed_at"], reverse=True)
    return claims


def should_suppress_cross_pipeline(rule_id: str, recent_claims: list[dict]) -> bool:
    """True — подавить reactivation-кандидат из-за недавней похвалы в ДРУГОМ конвейере.

    "Свежее живое наблюдение побеждает" (консенсус 02-writer.md): кандидат из
    правила в LIVE_SOURCE_RULES уже читает живой источник и сам является
    самым свежим наблюдением — недавняя похвала его не отменяет (иначе живой
    факт «активности нет» был бы заблокирован устаревшей похвалой — обратная
    версия того же инцидента). Для правил вне LIVE_SOURCE_RULES свежесть не
    доказана → fail-closed (S2): любая недавняя похвала подавляет упрёк.

    ВНИМАНИЕ: применять только к rule_id с canonical_type
    'engagement_reactivation' (см. filter_cross_pipeline_candidates) — для
    recognition-кандидатов вне LIVE_SOURCE_RULES эта функция вернёт True на
    собственной же похвале, что бессмысленно (ревью 08-peer-kimi.md).
    """
    if rule_id in LIVE_SOURCE_RULES:
        return False
    return any(c["claim"] == "high_activity" for c in recent_claims)


def filter_cross_pipeline_candidates(
    nudges: list[dict], recent_claims: list[dict], canonical_type_of: Callable[[str], str | None]
) -> list[dict]:
    """Применить should_suppress_cross_pipeline только к reactivation-кандидатам тика.

    Recognition-кандидаты (и любой нераспознанный rule_id) проходят без
    проверки — should_suppress_cross_pipeline имеет смысл только для упрёков
    об активности, не для похвалы (ревью 08-peer-kimi.md).
    """
    if not recent_claims:
        return nudges
    return [
        n for n in nudges
        if canonical_type_of(n["rule_id"]) != "engagement_reactivation"
        or not should_suppress_cross_pipeline(n["rule_id"], recent_claims)
    ]
