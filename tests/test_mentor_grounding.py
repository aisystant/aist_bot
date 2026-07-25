"""Unit-тесты grounding-гейта Наставника (WP-498 Ф5.1, DP.M.386).

Покрывает:
- RAG-поиск (mentor_grounding_search): mock gateway_mcp.knowledge_search —
  найден PD.METHOD.* выше порога → grounded; ниже порога / не найден /
  пустой результат / исключение поиска → not grounded.
- Grounding-инвариант (format_grounding_section): предметный ответ БЕЗ
  найденного источника обязан содержать явный запрет отвечать из общих
  знаний модели ("ЗАПРЕЩЕНО", "не нашёл") — не может быть тихо пропущен.
  Ответ С источником обязан явно требовать ссылку на конкретный PD.METHOD.id.
- Регистрация роли mentor в consultation_tools.py (_ROLE_FILES,
  ROLE_ATTRIBUTION, ROLE_TRANSITION, ROLE_CONTINUE_HINT) — по тому же
  паттерну, что navigator/diagnostician (DP.D.044).
- Промпт-файл config/prompts/mentor.md существует, непустой, содержит
  обязательные placeholders и явно описывает 4 роли-компонента связки.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from engines.shared.mentor_grounding import (  # noqa: E402
    GROUNDING_RELEVANCE_THRESHOLD,
    GroundingResult,
    format_grounding_section,
    mentor_grounding_search,
)


# =============================================================================
# mentor_grounding_search — RAG-гейт (шаг 1, DP.M.386)
# =============================================================================

@pytest.mark.asyncio
async def test_found_method_above_threshold_is_grounded():
    fake_results = [
        {
            "text": "Текст метода про застревание.",
            "source": "PD.METHOD.011-meme-audit.md",
            "score": GROUNDING_RELEVANCE_THRESHOLD + 0.2,
        }
    ]
    with patch(
        "engines.shared.mentor_grounding.gateway_mcp.knowledge_search",
        new=AsyncMock(return_value=fake_results),
    ):
        result = await mentor_grounding_search("застрял, что делать", telegram_user_id=123)

    assert result.grounded is True
    assert result.method_id == "PD.METHOD.011"
    assert "Текст метода" in result.text


@pytest.mark.asyncio
async def test_found_method_below_threshold_is_not_grounded():
    fake_results = [
        {
            "text": "Слабо релевантный текст.",
            "source": "PD.METHOD.050-precise-figure.md",
            "score": GROUNDING_RELEVANCE_THRESHOLD - 0.1,
        }
    ]
    with patch(
        "engines.shared.mentor_grounding.gateway_mcp.knowledge_search",
        new=AsyncMock(return_value=fake_results),
    ):
        result = await mentor_grounding_search("что-то смутное", telegram_user_id=123)

    # Метод найден (id известен для лога/отладки), но порог не пройден —
    # grounded обязан быть False, иначе grounding-инвариант нарушается.
    assert result.grounded is False


@pytest.mark.asyncio
async def test_results_without_pd_method_id_are_not_grounded():
    """Pack-документ есть, но это не PD.METHOD.* — не годится как источник (DP.SC.197)."""
    fake_results = [
        {
            "text": "Документ о другом домене платформы.",
            "source": "DP.ARCH.002-tiers.md",
            "score": 0.9,
        }
    ]
    with patch(
        "engines.shared.mentor_grounding.gateway_mcp.knowledge_search",
        new=AsyncMock(return_value=fake_results),
    ):
        result = await mentor_grounding_search("вопрос про тиры", telegram_user_id=123)

    assert result.grounded is False
    assert result.method_id is None


@pytest.mark.asyncio
async def test_empty_search_results_not_grounded():
    with patch(
        "engines.shared.mentor_grounding.gateway_mcp.knowledge_search",
        new=AsyncMock(return_value=[]),
    ):
        result = await mentor_grounding_search("вопрос без ответа", telegram_user_id=123)

    assert result.grounded is False
    assert result.method_id is None


@pytest.mark.asyncio
async def test_search_exception_degrades_to_not_grounded():
    """Технический сбой поиска → честная деградация (grounded=False), не исключение наружу."""
    with patch(
        "engines.shared.mentor_grounding.gateway_mcp.knowledge_search",
        new=AsyncMock(side_effect=RuntimeError("gateway down")),
    ):
        result = await mentor_grounding_search("вопрос", telegram_user_id=123)

    assert result.grounded is False


@pytest.mark.asyncio
async def test_empty_question_not_grounded_without_search_call():
    mock_search = AsyncMock(return_value=[{"source": "PD.METHOD.001", "score": 1.0, "text": "x"}])
    with patch("engines.shared.mentor_grounding.gateway_mcp.knowledge_search", new=mock_search):
        result = await mentor_grounding_search("   ", telegram_user_id=123)

    assert result.grounded is False
    mock_search.assert_not_called()


@pytest.mark.asyncio
async def test_best_score_wins_among_multiple_pd_methods():
    fake_results = [
        {"source": "PD.METHOD.011", "score": 0.4, "text": "слабее"},
        {"source": "PD.METHOD.033", "score": 0.8, "text": "сильнее"},
    ]
    with patch(
        "engines.shared.mentor_grounding.gateway_mcp.knowledge_search",
        new=AsyncMock(return_value=fake_results),
    ):
        result = await mentor_grounding_search("вопрос", telegram_user_id=123)

    assert result.method_id == "PD.METHOD.033"
    assert result.grounded is True


# =============================================================================
# format_grounding_section — grounding-инвариант (DP.SC.197 §Инварианты)
# =============================================================================

def test_format_grounding_section_with_source_requires_citation():
    result = GroundingResult(
        grounded=True, method_id="PD.METHOD.011", text="текст метода", source="s", score=0.7
    )
    section = format_grounding_section(result, lang="ru")

    assert "PD.METHOD.011" in section
    assert "ОБЯЗАН" in section
    assert "текст метода" in section


def test_format_grounding_section_without_source_forbids_general_knowledge():
    """Инвариант: без источника — явный запрет отвечать из общих знаний модели."""
    result = GroundingResult(grounded=False)
    section = format_grounding_section(result, lang="ru")

    assert "не найден" in section.lower() or "не найдена" in section.lower()
    assert "ЗАПРЕЩЕНО" in section
    # Не должен содержать ссылку на несуществующий метод
    assert "PD.METHOD." not in section.replace("PD.METHOD.*", "")


def test_format_grounding_section_grounded_true_but_no_method_id_falls_back_to_refusal():
    """Defensive: если grounded=True выставлен без method_id (не должно случаться
    из mentor_grounding_search, но проверяем контракт format_grounding_section
    отдельно) — секция обязана деградировать в честный отказ, не в пустую цитату."""
    result = GroundingResult(grounded=True, method_id=None)
    section = format_grounding_section(result, lang="ru")

    assert "ЗАПРЕЩЕНО" in section


# =============================================================================
# Регистрация роли mentor (DP.D.044 pattern reuse)
# =============================================================================

def test_role_dicts_have_mentor_entry_like_navigator_and_diagnostician():
    from engines.shared.consultation_tools import (
        ROLE_ATTRIBUTION,
        ROLE_CONTINUE_HINT,
        ROLE_TRANSITION,
        _ROLE_FILES,
    )

    for registry in (_ROLE_FILES, ROLE_ATTRIBUTION, ROLE_CONTINUE_HINT, ROLE_TRANSITION):
        assert "mentor" in registry
        assert "navigator" in registry
        assert "diagnostician" in registry


def test_load_role_prompt_mentor_returns_nonempty_template():
    from engines.shared.consultation_tools import load_role_prompt

    prompt = load_role_prompt("mentor")
    assert prompt is not None
    assert len(prompt) > 500

    # Обязательные placeholders (тот же контракт, что navigator.md/diagnostician.md)
    for placeholder in (
        "{name}", "{user_profile}", "{lang_instruction}",
        "{format_rules}", "{ontology_rules}", "{knowledge_section}",
        "{bot_section}", "{lang_reminder}",
    ):
        assert placeholder in prompt, f"missing {placeholder}"

    # 4 роли-компонента связки должны быть названы явно (не «изобретай новый»)
    for component_marker in ("Диагност", "Навигатор", "Преподаватель-лидер", "Преподаватель-предметник"):
        assert component_marker in prompt

    # Grounding-инвариант должен быть сформулирован в самом промпте (не только
    # в приложенном отдельно блоке) — модель должна знать правило заранее.
    assert "GROUNDING" in prompt


def test_handle_question_with_tools_accepts_role_context_extra_param():
    """Контракт: question_handler.handle_question_with_tools принимает
    role_context_extra (WP-498 Ф5.1) как keyword-only опциональный параметр."""
    import inspect

    from engines.shared.question_handler import handle_question_with_tools

    sig = inspect.signature(handle_question_with_tools)
    assert "role_context_extra" in sig.parameters
    assert sig.parameters["role_context_extra"].default is None
