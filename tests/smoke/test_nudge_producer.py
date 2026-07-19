"""Smoke-тесты core.nudge_producer (WP-117 Ф-decouple).

Проверяем наблюдаемое поведение: taxonomy-derivation, оба живых
state-predicate (active_today подавляет "return", first_use_connect_full
подавляет "onboarding"), payload-форма NudgeCandidate — не «импортировалось
и не упало».
"""
import core.nudge_producer as np
from core.nudge_delivery import NudgeCandidate


def _analyze_result(rule_id, nudge_key):
    return {"rule_id": rule_id, "nudge_key": nudge_key, "nudge_payload": {}, "cooldown_days": 7}


def test_produces_candidate_with_correct_payload_shape():
    nudges = [_analyze_result("inactivity_3d", "nudge_inactivity")]
    result = np.produce(
        nudges, user_id=42, text_by_nudge_key={"nudge_inactivity": "Мы вас ждём"},
        active_today=False, first_use_connect_full=False,
    )
    assert result == [
        NudgeCandidate(
            user_id=42, nudge_type="nudge_inactivity",
            payload={"text": "Мы вас ждём", "format": "markdown"},
            dedup_key="nudge:42:nudge_inactivity", priority=4,
        )
    ]


def test_return_category_suppressed_when_active_today():
    nudges = [_analyze_result("marathon_stalled", "nudge_marathon_stalled")]
    result = np.produce(
        nudges, user_id=1, text_by_nudge_key={"nudge_marathon_stalled": "Вернитесь"},
        active_today=True, first_use_connect_full=False,
    )
    assert result == []


def test_return_category_produced_when_not_active_today():
    nudges = [_analyze_result("marathon_stalled", "nudge_marathon_stalled")]
    result = np.produce(
        nudges, user_id=1, text_by_nudge_key={"nudge_marathon_stalled": "Вернитесь"},
        active_today=False, first_use_connect_full=False,
    )
    assert len(result) == 1
    assert result[0].nudge_type == "nudge_marathon_stalled"


def test_onboarding_category_suppressed_when_ai_client_connected():
    np._RULE_CATEGORY["onboarding_gap"] = "onboarding"
    try:
        nudges = [_analyze_result("onboarding_gap", "nudge_onboarding_gap")]
        result = np.produce(
            nudges, user_id=1, text_by_nudge_key={"nudge_onboarding_gap": "Подключите ИИ"},
            active_today=False, first_use_connect_full=True,
        )
        assert result == []
    finally:
        del np._RULE_CATEGORY["onboarding_gap"]


def test_missing_text_drops_candidate_without_inventing_content():
    nudges = [_analyze_result("inactivity_3d", "nudge_inactivity")]
    result = np.produce(
        nudges, user_id=1, text_by_nudge_key={},  # no text supplied
        active_today=False, first_use_connect_full=False,
    )
    assert result == []


def test_multiple_nudges_all_produced_independently():
    nudges = [
        _analyze_result("inactivity_3d", "nudge_inactivity"),
        _analyze_result("stage_upgrade", "nudge_stage_reached_2"),
    ]
    result = np.produce(
        nudges, user_id=7,
        text_by_nudge_key={"nudge_inactivity": "a", "nudge_stage_reached_2": "b"},
        active_today=False, first_use_connect_full=False,
    )
    assert {c.nudge_type for c in result} == {"nudge_inactivity", "nudge_stage_reached_2"}
