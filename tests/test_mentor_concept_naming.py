"""Unit-тесты писателя «Наставник называет понятие» (WP-498 Ф11, событие concept_named).

Покрывает:
- под-интент concept_naming: якорные фразы без префикса, расширенный список
  только после префикса «Наставник, …», отсутствие ложных срабатываний на
  «застрял»/Навигатор/Диагност, лексическая непересекаемость якорей с
  паттернами двух других ролей;
- маркер CONCEPT_NAMED: снятие из текста, один/ноль/два маркера;
- три проверки перед событием: словарь, русское название в ответе, чужой код;
- событие: конверт post_event (payload без PII, external_id по дню),
  T0 без аккаунта → событие не отправляется, session_type по умолчанию и
  при неизвестном значении;
- промпт: блок словаря содержит все понятия по-русски с кодом в скобках и
  запрет маркера «на всякий случай»; mentor.md описывает сценарий 9.
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from engines.shared.mentor_concept_naming import (  # noqa: E402
    CONCEPT_FACT_MAP,
    CONCEPT_MARKER_PREFIX,
    DEFAULT_SESSION_TYPE,
    build_external_id,
    emit_concept_named,
    extract_concept_marker,
    format_concept_naming_section,
    resolve_session_type,
    validate_named_concept,
)
from states.common.consultation import (  # noqa: E402
    _CONCEPT_NAMING_PATTERNS,
    _CONCEPT_NAMING_PREFIXED_PATTERNS,
    _DIAGNOSTICIAN_PATTERNS,
    _MENTOR_PATTERNS,
    _NAVIGATOR_PATTERNS,
    MENTOR_INTENT_CONCEPT_NAMING,
    _detect_role,
    detect_mentor_intent,
)


# =============================================================================
# Под-интент и маршрутизация
# =============================================================================

@pytest.mark.parametrize("q", [
    "Объясни через понятия IWE, что мы сейчас сделали",
    "какое понятие мы тут применили?",
    "Какой гейт сработал в этой работе?",
    "Назови понятие, которое я применил",
    "какие понятия IWE тут были",
])
def test_anchor_phrase_routes_to_concept_naming(q):
    assert detect_mentor_intent(q) == MENTOR_INTENT_CONCEPT_NAMING
    assert _detect_role(q) == "mentor"


@pytest.mark.parametrize("q", [
    "Наставник, что нового мы сделали?",
    "наставник: какой метод я применил в этой сессии",
    "Mentor, что мы сейчас сделали",
])
def test_extended_phrase_needs_prefix(q):
    assert detect_mentor_intent(q) == MENTOR_INTENT_CONCEPT_NAMING
    assert _detect_role(q) == "mentor"


@pytest.mark.parametrize("q", [
    "что нового мы сделали?",
    "какой метод я применил?",
    "что мы сейчас сделали",
    "какой гейт блокирует создание нового репо?",
])
def test_ambiguous_phrase_without_prefix(q):
    # Без префикса «метод»/«что сделали» — рефлексия, не интент называния
    # (семантическое пересечение с Навигатором, пир-сессия 2026-09-05-06).
    assert detect_mentor_intent(q) is None
    assert _detect_role(q) != "mentor"


@pytest.mark.parametrize("q", [
    "Наставник, я застрял на задании",
    "я в тупике и не знаю что делать",
])
def test_stuck_intent_stays_plain(q):
    assert _detect_role(q) == "mentor"
    assert detect_mentor_intent(q) is None


def test_other_roles_unaffected():
    assert _detect_role("Навигатор, с чего начать?") == "navigator"
    assert _detect_role("какой курс мне выбрать") == "navigator"
    assert _detect_role("Диагност, определи мою ступень") == "diagnostician"
    assert _detect_role("какая у меня ступень") == "diagnostician"
    assert _detect_role("Как оформить отпуск?") is None


def test_anchors_disjoint_from_other_lexicons():
    others = _NAVIGATOR_PATTERNS + _DIAGNOSTICIAN_PATTERNS + _MENTOR_PATTERNS
    for anchor in _CONCEPT_NAMING_PREFIXED_PATTERNS:
        for other in others:
            assert other not in anchor and anchor not in other, (anchor, other)
    assert set(_CONCEPT_NAMING_PATTERNS) <= set(_CONCEPT_NAMING_PREFIXED_PATTERNS)


# =============================================================================
# Маркер CONCEPT_NAMED
# =============================================================================

def test_marker_stripped_and_code_returned():
    answer = "Ты применил АрхГейт (AR.003): оценил решение по семи характеристикам.\n\nCONCEPT_NAMED: AR.003"
    clean, code = extract_concept_marker(answer)
    assert code == "AR.003"
    assert CONCEPT_MARKER_PREFIX not in clean
    assert clean.endswith("характеристикам.")


def test_marker_absent():
    text = "Не вижу применённого понятия. О какой работе речь?"
    clean, code = extract_concept_marker(text)
    assert code is None
    assert clean == text


def test_two_markers_ambiguous():
    answer = "Стоп-краны и АрхГейт.\nCONCEPT_NAMED: AR.D.003\nCONCEPT_NAMED: AR.003"
    clean, code = extract_concept_marker(answer)
    assert code is None
    assert "CONCEPT_NAMED" not in clean


@pytest.mark.parametrize("tail", [
    "**CONCEPT_NAMED: AR.003**",
    "*CONCEPT_NAMED: AR.003*",
    "`CONCEPT_NAMED: AR.003`",
    "- CONCEPT_NAMED: AR.003",
    "> CONCEPT_NAMED: AR.003",
    "CONCEPT_NAMED : AR.003",
    "CONCEPT_NAMED: AR.003.",
    "  CONCEPT_NAMED: AR.003  ",
    "CONCEPT_NAMED: AR.003\r\n",
])
def test_marker_tolerates_model_formatting(tail):
    # Холодное ревью 05.09 (Critical): без терпимости к оформлению строка
    # не снималась и утекала участнику дословно.
    answer = "Это АрхГейт (AR.003).\n" + tail
    clean, code = extract_concept_marker(answer)
    assert code == "AR.003"
    assert "CONCEPT_NAMED" not in clean
    assert clean == "Это АрхГейт (AR.003)."


def test_marker_inside_sentence_ignored():
    answer = "В коде маркер выглядит как CONCEPT_NAMED: AR.003 и снимается."
    clean, code = extract_concept_marker(answer)
    assert code is None
    assert clean == answer


# =============================================================================
# Три проверки перед событием
# =============================================================================

def test_validate_named_from_dictionary():
    entry = validate_named_concept("AR.D.003", "Ты остановился перед действием — это стоп-краны (AR.D.003).")
    assert entry is not None
    assert entry.fact_id == "М11"


def test_validate_inflected_form():
    entry = validate_named_concept("AR.D.004", "Разбор «что сделал ИИ, что я сам» — экзоскелетный режим (AR.D.004).")
    assert entry is not None
    assert entry.fact_id == "М14"


def test_validate_rejects_unnamed():
    assert validate_named_concept("AR.003", "Ты хорошо поработал, продолжай.") is None


def test_validate_rejects_unknown_code():
    assert validate_named_concept("DP.M.005", "Это АрхГейт (DP.M.005).") is None


def test_validate_rejects_none():
    assert validate_named_concept(None, "АрхГейт") is None


def test_dictionary_self_consistent():
    for code, entry in CONCEPT_FACT_MAP.items():
        assert entry.answer_markers, code
        assert entry.fact_id.startswith("М"), code
        assert validate_named_concept(code, f"Это {entry.name_ru} ({code}).") is entry, code
        assert code not in entry.name_ru, "код не должен подменять русское название"


# =============================================================================
# Событие
# =============================================================================

def test_external_id_per_day():
    day = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc)
    next_day = datetime(2026, 9, 6, 1, 0, tzinfo=timezone.utc)
    assert build_external_id("acc", "AR.003", day) == build_external_id("acc", "AR.003", evening)
    assert build_external_id("acc", "AR.003", day) != build_external_id("acc", "AR.003", next_day)
    assert build_external_id("acc", "AR.003", day) != build_external_id("acc", "AR.D.003", day)


def test_session_type_default_and_unknown():
    assert resolve_session_type({}) == DEFAULT_SESSION_TYPE == "work_session"
    assert resolve_session_type({"session_type": "self_development"}) == "self_development"
    assert resolve_session_type({"session_type": "lesson"}) == "work_session"


@pytest.mark.asyncio
async def test_emit_posts_schema_payload():
    entry = CONCEPT_FACT_MAP["AR.D.003"]
    with patch("engines.shared.mentor_concept_naming.resolve_ory_id_from_chat", new=AsyncMock(return_value="ory-uuid")), \
         patch("engines.shared.mentor_concept_naming.post_event", new=AsyncMock()) as post, \
         patch("engines.shared.mentor_concept_naming.asyncio.create_task", side_effect=lambda coro: coro.close()):
        scheduled = await emit_concept_named(42, "AR.D.003", entry, "work_session")
    assert scheduled is True
    kwargs = post.call_args.kwargs
    assert kwargs["source"] == "aist-bot"
    assert kwargs["event_type"] == "concept_named"
    assert kwargs["schema_version"] == "v1"
    assert kwargs["account_id"] == "ory-uuid"
    # Буквально тот payload, который принимает concept_named.v1.json (additionalProperties: false)
    assert kwargs["payload"] == {"concept_id": "AR.D.003", "session_type": "work_session", "channel": "bot"}
    assert kwargs["external_id"].startswith("concept-named-ory-uuid-AR.D.003-")


@pytest.mark.asyncio
async def test_emit_skips_without_account():
    entry = CONCEPT_FACT_MAP["AR.003"]
    with patch("engines.shared.mentor_concept_naming.resolve_ory_id_from_chat", new=AsyncMock(return_value=None)), \
         patch("engines.shared.mentor_concept_naming.post_event", new=AsyncMock()) as post:
        scheduled = await emit_concept_named(42, "AR.003", entry, "work_session")
    assert scheduled is False
    post.assert_not_called()


# =============================================================================
# Промпт
# =============================================================================

def test_prompt_section_s0_form():
    section = format_concept_naming_section("ru")
    for code, entry in CONCEPT_FACT_MAP.items():
        assert f"{entry.name_ru} ({code})" in section  # словарь для модели: название ↔ код маркера
    assert "участнику в тексте НЕ показывай" in section  # решение пилота 05.09: код не для участника
    assert CONCEPT_MARKER_PREFIX in section
    assert "ЗАПРЕЩЕНО" in section
    assert "пути к файлам" in section
    # Правила читаются моделью по порядку — номера обязаны идти подряд (ревью 05.09, Medium).
    rule_numbers = [int(line[0]) for line in section.splitlines() if line[:2] in {f"{n}." for n in range(1, 10)}]
    assert rule_numbers == list(range(1, len(rule_numbers) + 1)), rule_numbers


def test_mentor_prompt_has_scenario_9():
    from engines.shared.consultation_tools import load_role_prompt

    prompt = load_role_prompt("mentor")
    assert "Назови понятие" in prompt
    assert "коды понятий и документов участнику не показывай" in prompt
