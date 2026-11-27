"""Тест систематичности: четвёртое состояние «Развитие» и легенда состояний в результате."""
from core.assessment import (
    load_assessment,
    calculate_scores,
    get_dominant_group,
    get_max_per_group,
    format_result,
    format_states_legend,
)
from core.helpers import ASSESSMENT_BLOOM_MAP, _ASSESSMENT_ADAPTATIONS

LANGS = ("ru", "en", "es", "fr", "zh")
STATES = ["chaos", "deadlock", "turning_point", "development"]


def _a():
    a = load_assessment("systematicity")
    assert a
    return a


def test_four_groups_four_questions_each():
    a = _a()
    assert [g["id"] for g in a["groups"]] == STATES
    assert len(a["questions"]) == 16
    assert get_max_per_group(a) == 4
    for gid in STATES:
        assert sum(1 for q in a["questions"] if q["group"] == gid) == 4


def test_all_texts_translated():
    a = _a()
    for g in a["groups"]:
        for key in ("title", "description", "summary"):
            for lang in LANGS:
                assert g[key].get(lang), (g["id"], key, lang)
    for q in a["questions"]:
        for lang in LANGS:
            assert q["text"].get(lang), (q["id"], lang)
    for lang in LANGS:
        assert a["result_legend_title"][lang] and a["result_hint"][lang]
    assert {o["id"] for o in a["self_check"]["options"]} == set(STATES) | {"unsure"}


def test_development_can_dominate():
    a = _a()
    answers = {q["id"]: q["group"] == "development" for q in a["questions"]}
    scores = calculate_scores(a, answers)
    assert scores["development"] == 4
    assert get_dominant_group(a, scores)["id"] == "development"
    assert "Развитие" in format_result(a, scores, "ru")


def test_legend_lists_every_state_and_hint():
    a = _a()
    for lang in LANGS:
        legend = format_states_legend(a, lang)
        for g in a["groups"]:
            assert g["title"][lang] in legend
        assert a["result_hint"][lang] in legend


def test_legend_empty_without_summaries():
    assert format_states_legend({"groups": [{"id": "x", "title": {"ru": "X"}}]}, "ru") == ""


def test_state_is_known_to_content_adaptation():
    assert ASSESSMENT_BLOOM_MAP["development"] == 3
    assert "development" in _ASSESSMENT_ADAPTATIONS


def test_questions_alternate_between_states():
    a = _a()
    groups = [q["group"] for q in a["questions"]]
    assert all(x != y for x, y in zip(groups, groups[1:])), groups
    assert groups[:4] == STATES


def test_tie_is_shown_explicitly():
    a = _a()
    scores = {"chaos": 0, "deadlock": 3, "turning_point": 0, "development": 3}
    text = format_result(a, scores, "ru")
    assert "Поровну" in text and "Тупик" in text and "Развитие" in text
    assert "Преобладающее" not in text


def test_single_winner_has_no_tie_label():
    a = _a()
    scores = {"chaos": 1, "deadlock": 3, "turning_point": 0, "development": 2}
    text = format_result(a, scores, "ru")
    assert "Преобладающее состояние" in text and "Поровну" not in text


def test_tie_is_stored_as_mixed():
    from core.assessment import resolve_state_id, get_tied_groups, MIXED_STATE
    a = _a()
    scores = {"chaos": 0, "deadlock": 3, "turning_point": 0, "development": 3}
    assert resolve_state_id(a, scores) == MIXED_STATE
    assert [g["id"] for g in get_tied_groups(a, scores)] == ["deadlock", "development"]


def test_single_leader_and_all_zero_are_not_mixed():
    from core.assessment import resolve_state_id
    a = _a()
    assert resolve_state_id(a, {"chaos": 1, "deadlock": 3, "turning_point": 0, "development": 2}) == "deadlock"
    assert resolve_state_id(a, {g: 0 for g in STATES}) == "unsure"


def test_every_stored_state_has_a_label():
    from core.assessment import format_state_label
    for state in STATES + ["mixed", "unsure"]:
        for lang in LANGS:
            label = format_state_label(state, lang)
            assert label != state and label.strip(), (state, lang)
    assert format_state_label("mixed", "ru") == "🔀 Смешанное"
    assert format_state_label("no_such_state", "ru") == "no_such_state"


def test_feed_hints_know_development():
    from engines.feed.planner import _get_feed_topic_hint, _get_feed_digest_hint
    assert "Развитие" in _get_feed_topic_hint("development")
    assert "Развитие" in _get_feed_digest_hint("development")
    assert _get_feed_topic_hint("mixed") == "" and _get_feed_digest_hint("mixed") == ""
