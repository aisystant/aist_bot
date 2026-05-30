"""
Smoke-тесты WP-318 Ф11: /diagnose handler + cp_assessment queries + progress cp-section.

Проверяет:
1. Импорты без ошибок (handlers/diagnose.py, db/queries/cp_assessment.py)
2. compute_cp_stage() — чистая функция, bottleneck = argmin(mandatory)
3. DiagnoseStates — все FSM-состояния на месте
4. _show_cp_profile() — рендер с данными и без
5. Double-gate логика (cp_confirmed < bh_recommended → cp_gate_blocked)
"""

import sys
import os
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("KNOWLEDGE_MCP_URL", "https://fake-mcp.test/mcp")
os.environ.setdefault("USE_STATE_MACHINE", "false")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─── 1. Импорты ────────────────────────────────────────────

class TestDiagnoseImports:
    def test_import_handler(self):
        from handlers.diagnose import diagnose_router, DiagnoseStates
        assert diagnose_router is not None

    def test_import_cp_assessment_queries(self):
        from db.queries.cp_assessment import (
            compute_cp_stage,
            get_latest_cp_assessment,
            save_cp_assessment,
            MANDATORY_SLOTS,
            CP_TTL_DAYS,
        )
        # WP-370: align with PD.FORM.089 v5.0 — cp.iwe → informational, mandatory = 5.
        assert len(MANDATORY_SLOTS) == 5
        assert set(MANDATORY_SLOTS) == {"cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"}
        assert "cp.iwe" not in MANDATORY_SLOTS  # informational по v5.0
        assert CP_TTL_DAYS == 180

    def test_import_progress_labels(self):
        from states.utilities.progress import ProgressState
        assert hasattr(ProgressState, '_CP_STAGE_LABELS')
        assert hasattr(ProgressState, '_CP_SLOT_LABELS')
        assert len(ProgressState._CP_STAGE_LABELS) == 5
        assert len(ProgressState._CP_SLOT_LABELS) == 6


# ─── 2. compute_cp_stage() — чистая функция ───────────────

class TestComputeCpStage:
    def test_min_bottleneck(self):
        from db.queries.cp_assessment import compute_cp_stage
        scores = {"cp.rhy": 3, "cp.wld": 3, "cp.skl": 2, "cp.int": 3, "cp.agt": 3}
        result = compute_cp_stage(scores)
        assert result["stage"] == 2
        assert result["bottleneck_slot"] == "cp.skl"

    def test_all_same(self):
        from db.queries.cp_assessment import compute_cp_stage
        scores = {s: 3 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]}
        result = compute_cp_stage(scores)
        assert result["stage"] == 3
        assert result["recommended_stream"] == "S3"
        assert result["skip_to_stage"] == 3

    def test_recommended_stream_bounds(self):
        from db.queries.cp_assessment import compute_cp_stage
        scores_low = {s: 1 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]}
        scores_high = {s: 5 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]}
        assert compute_cp_stage(scores_low)["recommended_stream"] == "S1"
        # WP-371: stage=5 (Проактивный) → программа РР, не S4
        assert compute_cp_stage(scores_high)["recommended_stream"] == "РР"

    def test_stage_4_recommends_s4(self):
        """ст. 4 Дисциплинированный — последняя ступень с руководством S4."""
        from db.queries.cp_assessment import compute_cp_stage
        scores = {s: 4 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]}
        assert compute_cp_stage(scores)["recommended_stream"] == "S4"

    def test_stage_5_recommends_rr(self):
        """WP-371: ст. 5 Проактивный → программа Рабочего развития (РР) + след. роли Интеллектуал/Профессионал."""
        from db.queries.cp_assessment import compute_cp_stage
        scores = {s: 5 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]}
        result = compute_cp_stage(scores)
        assert result["stage"] == 5
        assert result["recommended_stream"] == "РР"

    def test_skip_to_stage_equals_stage(self):
        from db.queries.cp_assessment import compute_cp_stage
        scores = {"cp.rhy": 4, "cp.wld": 3, "cp.skl": 4, "cp.int": 4, "cp.agt": 4}
        result = compute_cp_stage(scores)
        assert result["skip_to_stage"] == result["stage"] == 3

    def test_missing_slot_defaults_to_1(self):
        """Отсутствующий слот трактуется как 1 (conservative default)."""
        from db.queries.cp_assessment import compute_cp_stage
        # cp.agt отсутствует
        scores = {"cp.rhy": 4, "cp.wld": 4, "cp.skl": 4, "cp.int": 4}
        result = compute_cp_stage(scores)
        assert result["stage"] == 1
        assert result["bottleneck_slot"] == "cp.agt"

    # WP-370 acceptance tests
    def test_all_max_gives_proactive_no_bottleneck(self):
        """5/5/5/5/5 → ступень 5 (Проактивный), bottleneck=None (нет узких мест), recommended РР (WP-371)."""
        from db.queries.cp_assessment import compute_cp_stage
        scores = {s: 5 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]}
        result = compute_cp_stage(scores)
        assert result["stage"] == 5
        assert result["bottleneck_slot"] is None  # WP-370: stage ≥4 → нет узких мест
        assert result["recommended_stream"] == "РР"  # WP-371: ст. 5 → РР, не S4

    def test_stage_4_no_bottleneck(self):
        """Все 4+ → bottleneck=None (порог «нет узких мест»)."""
        from db.queries.cp_assessment import compute_cp_stage
        scores = {s: 4 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]}
        assert compute_cp_stage(scores)["bottleneck_slot"] is None

    def test_stage_3_shows_bottleneck(self):
        """stage=3 — ещё показывает bottleneck."""
        from db.queries.cp_assessment import compute_cp_stage
        scores = {"cp.rhy": 3, "cp.wld": 5, "cp.skl": 5, "cp.int": 5, "cp.agt": 5}
        result = compute_cp_stage(scores)
        assert result["stage"] == 3
        assert result["bottleneck_slot"] == "cp.rhy"

    def test_cp_iwe_does_not_affect_stage(self):
        """WP-370: cp.iwe — informational, не входит в mandatory → не блокирует ступень."""
        from db.queries.cp_assessment import compute_cp_stage
        # все mandatory = 5, cp.iwe = 1 → stage всё равно 5
        scores = {"cp.rhy": 5, "cp.wld": 5, "cp.skl": 5, "cp.int": 5, "cp.agt": 5, "cp.iwe": 1}
        result = compute_cp_stage(scores)
        assert result["stage"] == 5
        assert result["bottleneck_slot"] is None


# ─── 3. DiagnoseStates FSM ─────────────────────────────────

class TestDiagnoseStates:
    def test_states_exist(self):
        from handlers.diagnose import DiagnoseStates
        assert hasattr(DiagnoseStates, 'q1')
        assert hasattr(DiagnoseStates, 'q2')
        assert hasattr(DiagnoseStates, 'q3')
        assert hasattr(DiagnoseStates, 'q4')
        assert hasattr(DiagnoseStates, 'q5')
        assert hasattr(DiagnoseStates, 'q6')  # WP-370: Phase 2 drill-down

    def test_phase1_questions_count(self):
        from handlers.diagnose import PHASE1_QUESTIONS
        # WP-370: 5 вопросов = 4 mandatory якорных + 1 informational (cp.iwe)
        assert len(PHASE1_QUESTIONS) == 5

    def test_phase1_covers_4_mandatory_anchors(self):
        """WP-370: PHASE1 должна содержать 4 mandatory якорных (cp.rhy/wld/int/agt) + cp.iwe.
        cp.skl derive из cp.rhy в _finish_diagnose."""
        from handlers.diagnose import PHASE1_QUESTIONS
        slots = [q["slot"] for q in PHASE1_QUESTIONS]
        assert "cp.rhy" in slots
        assert "cp.wld" in slots
        assert "cp.int" in slots
        assert "cp.agt" in slots
        assert "cp.iwe" in slots  # informational
        assert "cp.skl" not in slots  # derive из cp.rhy

    def test_phase2_questions_cover_all_mandatory(self):
        from handlers.diagnose import PHASE2_QUESTIONS
        from db.queries.cp_assessment import MANDATORY_SLOTS
        # Phase 2 drill-down должен покрывать все 5 mandatory слотов
        for slot in MANDATORY_SLOTS:
            assert slot in PHASE2_QUESTIONS, f"Missing drill-down for {slot}"

    def test_min_anchors_constant(self):
        """WP-370: MIN_ANCHORS = 4 (все mandatory якорные)."""
        from handlers.diagnose import MIN_ANCHORS
        assert MIN_ANCHORS == 4


# ─── 4. _show_cp_profile() rendering ──────────────────────

class TestShowCpProfile:
    def _make_state(self):
        from states.utilities.progress import ProgressState
        state = ProgressState.__new__(ProgressState)
        return state

    @pytest.mark.asyncio
    async def test_renders_no_profile(self):
        """Без cp-профиля — показывает приглашение пройти диагностику."""
        state = self._make_state()
        sent = []

        async def fake_show_section(user, text, keyboard, callback=None):
            sent.append(text)

        state._show_section = fake_show_section

        fake_user = {"chat_id": 123, "language": "ru", "current_context": "{}"}
        await state._show_cp_profile(fake_user, {"cp_profile": None}, "ru")
        assert sent, "Должен отправить сообщение"
        assert "/diagnose" in sent[0], "Должен упомянуть /diagnose"

    @pytest.mark.asyncio
    async def test_renders_with_profile(self):
        """С cp-профилем — показывает ступень, bottleneck, поток."""
        state = self._make_state()
        sent = []

        async def fake_show_section(user, text, keyboard, callback=None):
            sent.append(text)

        state._show_section = fake_show_section

        cp = {
            "stage": 2,
            "bottleneck_slot": "cp.skl",
            "recommended_stream": "S2",
            "valid_until": "2026-11-16T12:00:00+00:00",
            "cp_scores": {
                "cp.rhy": 3, "cp.wld": 3, "cp.skl": 2,
                "cp.iwe": 3, "cp.int": 3, "cp.agt": 3,
            },
        }
        fake_user = {"chat_id": 123, "language": "ru", "current_context": "{}"}
        await state._show_cp_profile(fake_user, {"cp_profile": cp}, "ru")

        assert sent, "Должен отправить сообщение"
        text = sent[0]
        assert "Практикующий" in text, "Должен показать название ступени"
        assert "Навыки" in text, "Должен показать bottleneck по-русски (cp.skl→Навыки)"
        assert "S2" in text, "Должен показать рекомендованный поток"
        assert "2026-11-16" in text, "Должен показать дату valid_until"
        assert "●" in text, "Должен показать bar-профиль"

    @pytest.mark.asyncio
    async def test_valid_until_none_safe(self):
        """valid_until = None не должен вызывать TypeError."""
        state = self._make_state()
        sent = []

        async def fake_show_section(user, text, keyboard, callback=None):
            sent.append(text)

        state._show_section = fake_show_section

        cp = {
            "stage": 3,
            "bottleneck_slot": "cp.iwe",
            "recommended_stream": "S3",
            "valid_until": None,
            "cp_scores": {},
        }
        fake_user = {"chat_id": 123, "language": "ru", "current_context": "{}"}
        # Не должен упасть
        await state._show_cp_profile(fake_user, {"cp_profile": cp}, "ru")
        assert sent
        assert "—" in sent[0], "Должен показать '—' вместо даты"


# ─── 5. Double-gate: cp_gate_blocked ──────────────────────

class TestDoubleGate:
    """Верификация логики двойного gate (WP-318 Ф4, FORM.089 §5.1)."""

    def test_cp_gate_blocks_higher_bh(self):
        """cp_confirmed(2) < bh_recommended(3) → action cp_gate_blocked."""
        from db.queries.cp_assessment import compute_cp_stage

        # Аттестатор хочет повысить до ст. 3 по bh-метрикам
        bh_recommended = 3

        # cp-диагностика подтверждает только ст. 2
        cp_scores = {"cp.rhy": 2, "cp.wld": 3, "cp.skl": 2, "cp.iwe": 3, "cp.int": 3, "cp.agt": 3}
        cp_stage = compute_cp_stage(cp_scores)["stage"]

        # Двойной gate: min(bh, cp)
        confirmed = min(bh_recommended, cp_stage)
        assert confirmed == 2, "Gate должен заблокировать повышение до ст. 3"

    def test_cp_gate_allows_equal(self):
        """cp_confirmed == bh_recommended → переход разрешён."""
        bh_recommended = 2
        cp_scores = {s: 2 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.iwe", "cp.int", "cp.agt"]}
        from db.queries.cp_assessment import compute_cp_stage
        cp_stage = compute_cp_stage(cp_scores)["stage"]
        confirmed = min(bh_recommended, cp_stage)
        assert confirmed == 2

    def test_cp_gate_allows_lower_bh(self):
        """cp_confirmed(3) >= bh_recommended(2) → переход по bh разрешён."""
        bh_recommended = 2
        cp_scores = {s: 3 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"]}
        from db.queries.cp_assessment import compute_cp_stage
        cp_stage = compute_cp_stage(cp_scores)["stage"]
        confirmed = min(bh_recommended, cp_stage)
        assert confirmed == 2


# ─── 6. WP-370: Pack-sync drift detector ──────────────────

class TestPackSyncDrift:
    """Анти-drift тест: MANDATORY_SLOTS должен совпадать с PD.FORM.089 §2 «Стержневые для расчёта ступени».

    Source: PACK-personal/.../formalizations/PD.FORM.089-learner-rcs.md
    Если spec изменится → этот тест должен fail и заставить синхронизировать.
    """

    EXPECTED_FROM_FORM089 = {"cp.rhy", "cp.wld", "cp.skl", "cp.int", "cp.agt"}

    def test_mandatory_matches_form089(self):
        from db.queries.cp_assessment import MANDATORY_SLOTS
        assert set(MANDATORY_SLOTS) == self.EXPECTED_FROM_FORM089, (
            "MANDATORY_SLOTS дрейфует от PD.FORM.089 v5.0 §2 «Стержневые для расчёта ступени». "
            "Обнови один из двух источников или зафиксируй drift в issue."
        )

    def test_cp_iwe_informational_not_mandatory(self):
        """v5.0 явно: cp.iwe и cp.cre — informational. Не должны быть в MANDATORY_SLOTS."""
        from db.queries.cp_assessment import MANDATORY_SLOTS
        assert "cp.iwe" not in MANDATORY_SLOTS
        assert "cp.cre" not in MANDATORY_SLOTS

    def test_phase1_anchors_match_form089_table(self):
        """PHASE1 якорные = 4 mandatory (cp.skl derive) + 1 informational (cp.iwe).

        Spec §6.1 Фаза 1 таблица: cp.rhy, cp.wld, cp.iwe, cp.int.
        cp.skl выводится из cp.rhy.
        cp.agt — дополнительный вопрос (или bh.agn-прокси).

        Здесь интерпретация Plan A′: 4 mandatory anchors (rhy/wld/int/agt) + cp.iwe.
        """
        from handlers.diagnose import PHASE1_QUESTIONS
        slots = {q["slot"] for q in PHASE1_QUESTIONS}
        # 4 mandatory якорных (без cp.skl — derive)
        assert {"cp.rhy", "cp.wld", "cp.int", "cp.agt"}.issubset(slots)
        # informational
        assert "cp.iwe" in slots
        # cp.skl НЕ должен быть в PHASE1 — derive в _finish_diagnose
        assert "cp.skl" not in slots
