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
    original = np.get_rule_config
    class _Rule:
        opt_out_category = "onboarding"
    np.get_rule_config = lambda _rule_id: _Rule()
    try:
        nudges = [_analyze_result("onboarding_gap", "nudge_onboarding_gap")]
        result = np.produce(
            nudges, user_id=1, text_by_nudge_key={"nudge_onboarding_gap": "Подключите ИИ"},
            active_today=False, first_use_connect_full=True,
        )
        assert result == []
    finally:
        np.get_rule_config = original


def test_onboarder_gap_category_not_suppressed_by_ai_client_connected():
    """onboarder_gap (WP-406 Х2/Х3) — независимая категория от "onboarding"
    (T2→T3 AI-клиент). first_use_connect_full не должен её гасить: разрыв в
    понимании сообщества/выборе траектории не закрывается подключением ИИ."""
    nudges = [_analyze_result("onboarder_gap", "nudge_onboarder_gap_x2")]
    result = np.produce(
        nudges, user_id=1, text_by_nudge_key={"nudge_onboarder_gap_x2": "Освоиться"},
        active_today=False, first_use_connect_full=True,
    )
    assert len(result) == 1
    assert result[0].nudge_type == "nudge_onboarder_gap_x2"


def test_onboarder_gap_payload_carries_offer_button():
    """Текст нуджа обещает кнопку «Освоиться» (i18n schema.yaml nudge_onboarder_gap_x2/x3)
    — payload обязан нести actions, иначе _build_delivery_kwargs (core/scheduler.py)
    не построит reply_markup и кнопка не появится в сообщении (баг, найден 2026-07-29)."""
    nudges = [_analyze_result("onboarder_gap", "nudge_onboarder_gap_x2")]
    result = np.produce(
        nudges, user_id=1, text_by_nudge_key={"nudge_onboarder_gap_x2": "Освоиться"},
        active_today=False, first_use_connect_full=False,
    )
    assert len(result) == 1
    assert result[0].payload["actions"] == [{"label": "🎓 Освоиться", "action": "onboarder_start"}]


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
    assert {c.nudge_type for c in result} == {"nudge_inactivity"}


def test_stopgap_suppresses_achievement_sessions_in_producer():
    nudges = [_analyze_result("achievement_sessions", "nudge_sessions_10")]
    result = np.produce(
        nudges, user_id=1,
        text_by_nudge_key={"nudge_sessions_10": "10 сессий!"},
        active_today=False, first_use_connect_full=False,
    )
    assert result == []


def test_stopgap_suppresses_achievement_active_days_in_producer():
    nudges = [_analyze_result("achievement_active_days", "nudge_active_days_30")]
    result = np.produce(
        nudges, user_id=1,
        text_by_nudge_key={"nudge_active_days_30": "30 дней!"},
        active_today=False, first_use_connect_full=False,
    )
    assert result == []


def test_stopgap_suppresses_stage_upgrade_in_producer():
    nudges = [_analyze_result("stage_upgrade", "nudge_stage_reached_2")]
    result = np.produce(
        nudges, user_id=1,
        text_by_nudge_key={"nudge_stage_reached_2": "Ступень 2"},
        active_today=False, first_use_connect_full=False,
    )
    assert result == []


def test_stopgap_preserves_agency_high_in_producer():
    """agency_high — не achievement, producer не должен его подавлять."""
    nudges = [_analyze_result("agency_high", "nudge_agency_high")]
    result = np.produce(
        nudges, user_id=1,
        text_by_nudge_key={"nudge_agency_high": "Высокая агентность"},
        active_today=False, first_use_connect_full=False,
    )
    assert len(result) == 1
    assert result[0].nudge_type == "nudge_agency_high"


def test_stopgap_mixed_list_keeps_allowed_nudges():
    nudges = [
        _analyze_result("inactivity_3d", "nudge_inactivity"),
        _analyze_result("achievement_sessions", "nudge_sessions_25"),
        _analyze_result("agency_high", "nudge_agency_high"),
    ]
    result = np.produce(
        nudges, user_id=1,
        text_by_nudge_key={
            "nudge_inactivity": "a",
            "nudge_sessions_25": "b",
            "nudge_agency_high": "c",
        },
        active_today=False, first_use_connect_full=False,
    )
    assert {c.nudge_type for c in result} == {"nudge_inactivity", "nudge_agency_high"}


def test_narrative_reactivation_suppresses_recognition_before_delivery_filters():
    nudges = [
        _analyze_result("inactivity_3d", "nudge_inactivity"),
        _analyze_result("agency_high", "nudge_agency_high"),
        _analyze_result("low_regularity", "nudge_low_regularity"),
    ]
    result = np.arbitrate_narrative(nudges)
    assert {n["nudge_key"] for n in result} == {
        "nudge_inactivity",
        "nudge_low_regularity",
    }


def test_narrative_activity_event_reopens_recognition_without_ttl():
    nudges = [_analyze_result("agency_high", "nudge_agency_high")]
    assert np.arbitrate_narrative(nudges) == nudges


def test_arbitrate_narrative_unmapped_rule_id_does_not_crash():
    # peer-review round 1: незамапленный rule_id не должен ронять KeyError'ом
    # весь тик — нудж проходит без арбитража (безопасный дефолт).
    nudges = [_analyze_result("ghost_rule", "nudge_ghost")]
    assert np.arbitrate_narrative(nudges) == nudges


def test_arbitrate_narrative_unmapped_survives_alongside_reactivation():
    # peer-review round 2: путь, где реально происходит арбитраж —
    # reactivation-нудж есть, а рядом незамапленный rule_id. Оба переживают
    # фильтрацию (незамапленный ≠ recognition, значит не подавляется).
    nudges = [
        _analyze_result("inactivity_3d", "nudge_inactivity"),
        _analyze_result("ghost_rule", "nudge_ghost"),
    ]
    result = np.arbitrate_narrative(nudges)
    assert {n["nudge_key"] for n in result} == {"nudge_inactivity", "nudge_ghost"}


def test_produce_unmapped_rule_id_falls_back_to_engagement():
    # peer-review round 2: active_today=True — если бы fallback был 'return',
    # нудж был бы подавлен гейтом; 'engagement' — проходит.
    result = np.produce(
        [_analyze_result("ghost_rule", "nudge_ghost")],
        user_id=1,
        text_by_nudge_key={"nudge_ghost": "текст"},
        active_today=True,
        first_use_connect_full=False,
    )
    assert [c.nudge_type for c in result] == ["nudge_ghost"]
