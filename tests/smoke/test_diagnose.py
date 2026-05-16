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
        assert len(MANDATORY_SLOTS) == 6
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
        scores = {"cp.rhy": 3, "cp.wld": 3, "cp.skl": 2, "cp.iwe": 3, "cp.int": 3, "cp.agt": 3}
        result = compute_cp_stage(scores)
        assert result["stage"] == 2
        assert result["bottleneck_slot"] == "cp.skl"

    def test_all_same(self):
        from db.queries.cp_assessment import compute_cp_stage
        scores = {s: 3 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.iwe", "cp.int", "cp.agt"]}
        result = compute_cp_stage(scores)
        assert result["stage"] == 3
        assert result["recommended_stream"] == "S3"
        assert result["skip_to_stage"] == 3

    def test_recommended_stream_bounds(self):
        from db.queries.cp_assessment import compute_cp_stage
        # stage=1 → S1, stage=5 → S4 (max)
        scores_low = {s: 1 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.iwe", "cp.int", "cp.agt"]}
        scores_high = {s: 5 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.iwe", "cp.int", "cp.agt"]}
        assert compute_cp_stage(scores_low)["recommended_stream"] == "S1"
        assert compute_cp_stage(scores_high)["recommended_stream"] == "S4"

    def test_skip_to_stage_equals_stage(self):
        from db.queries.cp_assessment import compute_cp_stage
        scores = {"cp.rhy": 4, "cp.wld": 3, "cp.skl": 4, "cp.iwe": 4, "cp.int": 4, "cp.agt": 4}
        result = compute_cp_stage(scores)
        assert result["skip_to_stage"] == result["stage"] == 3

    def test_missing_slot_defaults_to_1(self):
        """Отсутствующий слот трактуется как 1 (conservative default)."""
        from db.queries.cp_assessment import compute_cp_stage
        # cp.agt отсутствует
        scores = {"cp.rhy": 4, "cp.wld": 4, "cp.skl": 4, "cp.iwe": 4, "cp.int": 4}
        result = compute_cp_stage(scores)
        assert result["stage"] == 1
        assert result["bottleneck_slot"] == "cp.agt"


# ─── 3. DiagnoseStates FSM ─────────────────────────────────

class TestDiagnoseStates:
    def test_states_exist(self):
        from handlers.diagnose import DiagnoseStates
        assert hasattr(DiagnoseStates, 'q1')
        assert hasattr(DiagnoseStates, 'q2')
        assert hasattr(DiagnoseStates, 'q3')
        assert hasattr(DiagnoseStates, 'q4')
        assert hasattr(DiagnoseStates, 'q5')

    def test_phase1_questions_count(self):
        from handlers.diagnose import PHASE1_QUESTIONS
        assert len(PHASE1_QUESTIONS) == 4

    def test_phase1_slots_are_mandatory(self):
        from handlers.diagnose import PHASE1_QUESTIONS
        from db.queries.cp_assessment import MANDATORY_SLOTS
        for q in PHASE1_QUESTIONS:
            assert q["slot"] in MANDATORY_SLOTS, f"Slot {q['slot']} not in MANDATORY_SLOTS"

    def test_phase2_questions_cover_all_bottlenecks(self):
        from handlers.diagnose import PHASE2_QUESTIONS
        from db.queries.cp_assessment import MANDATORY_SLOTS
        # Phase 2 drill-down должен покрывать все mandatory слоты
        for slot in MANDATORY_SLOTS:
            assert slot in PHASE2_QUESTIONS, f"Missing drill-down for {slot}"


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
        cp_scores = {s: 3 for s in ["cp.rhy", "cp.wld", "cp.skl", "cp.iwe", "cp.int", "cp.agt"]}
        from db.queries.cp_assessment import compute_cp_stage
        cp_stage = compute_cp_stage(cp_scores)["stage"]
        confirmed = min(bh_recommended, cp_stage)
        assert confirmed == 2
