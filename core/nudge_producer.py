from __future__ import annotations

"""
NudgeProducer — WP-117 Ф-decouple deliverable (contract: DS-my-strategy/
inbox/WP-117/f-decouple-contract.md).

Boundary (contract §1): this module decides WHICH candidates to produce from
engagement_analyzer.analyze() output; core.nudge_delivery (WP-418) decides
WHETHER/HOW to deliver them (cooldown, class cap, opt-out, channel).

Adapted from the design prototype (DS-my-strategy/inbox/WP-117/
nudge_producer_seam.py, self-tested 2026-06-21) to the real rule_id vocabulary
and NudgeCandidate/payload shape of core.nudge_delivery.

Scope cut (pilot decision, 2026-07-19): the prototype's Navigator suppression
gate (nudge_policy_fact read from public.domain_event) and the "acted within
24h" history predicate are NOT implemented here — neither has a live data
source yet (no nudge_policy_fact writer exists; notification_log.status is
hardcoded to "delivered", "acted" never occurs). Implementing either would be
dead code with no way to ever take the branch. What IS implemented: the two
state-predicates from f-decouple-contract.md §3 that already have a live
data source in get_nudge_candidates()/onboarding_state — the "return" nudge
suppressed for a currently-active user (21-apr incident class) and dropping
onboarding nudges once the AI client is connected.
"""

import logging

from config.nudge_registry import get_rule_config
from core.nudge_delivery import NudgeCandidate
from core.nudge_policy import stopgap_suppression_reason
from core.onboarder.offer import offer_payload

logger = logging.getLogger(__name__)


_REACTIVATION_TYPE = "engagement_reactivation"
_RECOGNITION_TYPE = "recognition_progress"


def arbitrate_narrative(nudges: list[dict]) -> list[dict]:
    """Suppress recognition while current rule facts say reactivation is needed.

    This runs on raw analyzer output before cooldown filtering. A fresh activity
    event makes the reactivation rule stop firing on the next run, which closes
    the suppression window without a second TTL.
    """
    has_reactivation = any(
        get_rule_config(n["rule_id"]).canonical_type == _REACTIVATION_TYPE
        for n in nudges
    )
    if not has_reactivation:
        return list(nudges)
    return [
        n
        for n in nudges
        if get_rule_config(n["rule_id"]).canonical_type != _RECOGNITION_TYPE
    ]


def produce(
    nudges: list[dict],
    user_id: int,
    text_by_nudge_key: dict[str, str],
    active_today: bool,
    first_use_connect_full: bool,
) -> list[NudgeCandidate]:
    """Turn engagement_analyzer.analyze() output into NudgeCandidate list.

    Args:
        nudges: analyze() output — [{rule_id, nudge_key, nudge_payload, cooldown_days}, ...]
        user_id: chat_id (candidate key in nudge_delivery)
        text_by_nudge_key: nudge_key -> already-built message text (static i18n
            or AI-personalized, per current send_engagement_nudges() pipeline).
            A nudge_key missing here is dropped — producer never invents text.
        active_today: user's last_active_date == MSK-today (state-predicate
            input; same МСК-calendar as engagement_analyzer._moscow_today()).
        first_use_connect_full: AI client connected (onboarding_state).

    Returns:
        NudgeCandidate list, cooldown/class_cap/opt-out left to nudge_delivery.
    """
    out: list[NudgeCandidate] = []
    for n in nudges:
        rule_id = n["rule_id"]
        nudge_key = n["nudge_key"]

        # WP-117 Ф-stopgap: defense-in-depth — analyzer должен был отфильтровать,
        # но если результат дошёл до producer напрямую, подавляем здесь тоже.
        stopgap_reason = stopgap_suppression_reason(rule_id, nudge_key)
        if stopgap_reason:
            logger.warning(
                "[NudgeProducer] Stopgap suppressed rule=%s nudge=%s reason=%s",
                rule_id, nudge_key, stopgap_reason
            )
            continue

        category = get_rule_config(rule_id).opt_out_category

        if category == "return" and active_today:
            continue  # 21-apr incident class: active user, no "come back".
        if category == "onboarding" and first_use_connect_full:
            continue  # gap closed: AI-client already connected.

        text = text_by_nudge_key.get(nudge_key)
        if not text:
            logger.warning(f"[NudgeProducer] No text for {nudge_key}, dropping candidate")
            continue

        payload: dict = {"text": text, "format": "markdown"}
        if category == "onboarder":
            offer = offer_payload()
            payload["actions"] = [{"label": offer["button_text"], "action": offer["callback_data"]}]

        out.append(
            NudgeCandidate(
                user_id=user_id,
                nudge_type=nudge_key,
                payload=payload,
                dedup_key=f"nudge:{user_id}:{nudge_key}",
                priority=4,
            )
        )
    return out
