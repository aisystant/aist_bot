"""Smoke-тесты AI-персонализации derived-aware nudges (WP-117 Ф-roles).

Проверяем наблюдаемое поведение: какие типы считаются personalizable,
что generate_derived_nudge_text возвращает при успехе/ошибке/пустом ответе Claude —
не «импортировалось и не упало».
"""
import pytest

import core.engagement_analyzer as ea
from config import CLAUDE_MODEL_HAIKU


# ─────────────────────────── is_ai_personalizable ───────────────────────────

def test_basic_rule_types_not_personalizable():
    assert ea.is_ai_personalizable("nudge_inactivity") is False
    assert ea.is_ai_personalizable("nudge_streak_drop") is False
    assert ea.is_ai_personalizable("nudge_sessions_10") is False
    assert ea.is_ai_personalizable("nudge_active_days_30") is False


def test_derived_rule_types_are_personalizable():
    assert ea.is_ai_personalizable("nudge_stage_reached_2") is True
    assert ea.is_ai_personalizable("nudge_agency_growing") is True
    assert ea.is_ai_personalizable("nudge_agency_high") is True
    assert ea.is_ai_personalizable("nudge_low_regularity") is True
    assert ea.is_ai_personalizable("nudge_reduce_frequency") is True
    assert ea.is_ai_personalizable("nudge_bottleneck_cp_rhy") is True


# ─────────────────────────── generate_derived_nudge_text ───────────────────────────

class _FakeClaude:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def generate(self, system_prompt, user_prompt, max_tokens=None, model=None):
        self.calls.append((system_prompt, user_prompt, max_tokens, model))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_claude(monkeypatch, response):
    import clients
    fake = _FakeClaude(response)
    monkeypatch.setattr(clients, "claude", fake, raising=False)
    return fake


@pytest.mark.asyncio
async def test_returns_personalized_text_on_success(monkeypatch):
    fake = _patch_claude(monkeypatch, "📈 Ты уже на ступени 2 — держи темп!")
    text = await ea.generate_derived_nudge_text(
        "nudge_stage_reached_2",
        "📈 Вы перешли на ступень *Систематический*!",
        user_meta={"active_days_streak": 5},
        derived={"3_4_qualification": {"stage": 2}},
        lang="ru",
    )
    assert text == "📈 Ты уже на ступени 2 — держи темп!"
    assert len(fake.calls) == 1
    assert fake.calls[0][3] == CLAUDE_MODEL_HAIKU


@pytest.mark.asyncio
async def test_falls_back_to_static_on_claude_exception(monkeypatch):
    _patch_claude(monkeypatch, RuntimeError("Claude API down"))
    static_text = "📈 Вы перешли на ступень *Систематический*!"
    text = await ea.generate_derived_nudge_text(
        "nudge_stage_reached_2", static_text,
        user_meta={}, derived={}, lang="ru",
    )
    assert text == static_text


@pytest.mark.asyncio
async def test_falls_back_to_static_on_empty_response(monkeypatch):
    _patch_claude(monkeypatch, "   ")
    static_text = "💎 Высокий индекс агентности!"
    text = await ea.generate_derived_nudge_text(
        "nudge_agency_high", static_text,
        user_meta={}, derived={}, lang="ru",
    )
    assert text == static_text
