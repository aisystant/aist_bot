"""Qualification instead of stage for «Работник» and above on every screen (WP-417 Ф8).

Stages 1..5 exist only inside «Ученик»; /progress got the rule in #430. These tests cover the
remaining screens: diagnostic profile, /twin, /me, «Мои данные», settings and the /diagnose result.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("KNOWLEDGE_MCP_URL", "https://fake-mcp.test/mcp")
os.environ.setdefault("USE_STATE_MACHINE", "false")

import pytest

from core.qualification import degree_from_profile, qualified_level

REFORMER = {"code": "L7", "level": "Реформатор", "numeric": 70, "source": "lms"}
STUDENT = {"level": "Ученик", "numeric": 20, "source": "lms"}
STAGE_5 = {"stage": 5}


def _profile(degree=None, lms_degree=None, stage=STAGE_5) -> dict:
    derived = {"3_4_qualification": stage} if stage else {}
    if degree is not None:
        derived["3_8_degree"] = degree
    collected = {"2_2_courses": {"qualification_level": lms_degree}} if lms_degree else {}
    return {"3_derived": derived, "2_collected": collected}


# ── core/qualification.py ──

@pytest.mark.parametrize("degree, expected", [
    (REFORMER, "Реформатор"),
    (dict(REFORMER, level="Работник", numeric=25), "Работник"),
    (dict(REFORMER, numeric="25"), "Реформатор"),         # numeric as text, as JSON may deliver it
    (STUDENT, None),
    (dict(REFORMER, numeric=24.9), None),
    (dict(REFORMER, numeric="inf"), None),
    (dict(REFORMER, numeric="nan"), None),
    (dict(REFORMER, numeric=None), None),
    (dict(REFORMER, level=""), None),
    (None, None),
    ("Реформатор", None),
])
def test_qualified_level_threshold(degree, expected):
    assert qualified_level(degree) == expected


def test_degree_from_profile_prefers_night_value_then_lms():
    assert degree_from_profile(_profile(degree=REFORMER, lms_degree=STUDENT)) == REFORMER
    # empty night value (new qualification before the night run): the LMS record
    assert degree_from_profile(_profile(degree={"level": ""}, lms_degree=REFORMER)) == REFORMER
    assert degree_from_profile({"_derived": {"3_8_degree": REFORMER}}) == REFORMER
    assert degree_from_profile(_profile()) is None
    assert degree_from_profile(None) is None


# ── /twin profile and /me dashboard ──

def test_twin_profile_hides_stage_for_reformer():
    from handlers.twin import _profile_text

    text = _profile_text(_profile(degree=REFORMER), "ru")

    assert "Реформатор" in text
    assert "Ступень" not in text and "(5/5)" not in text


def test_twin_profile_keeps_stage_for_student():
    from handlers.twin import _profile_text

    text = _profile_text(_profile(degree=STUDENT), "ru")

    assert "Проактивный (5/5)" in text


def test_me_dashboard_hides_stage_for_reformer_from_lms_record():
    from handlers.twin import _build_me_dashboard

    engagement = {"2_2_courses": {"qualification_level": REFORMER}, "_derived": {"3_4_qualification": STAGE_5}}
    text = _build_me_dashboard(engagement, {"name": "Иван"}, "ru")

    assert "Реформатор" in text
    assert "(5/5)" not in text


def test_me_dashboard_keeps_stage_for_student():
    from handlers.twin import _build_me_dashboard

    engagement = {"_derived": {"3_8_degree": STUDENT, "3_4_qualification": STAGE_5}}
    text = _build_me_dashboard(engagement, {"name": "Иван"}, "ru")

    assert "Проактивный (5/5)" in text


# ── «Мои данные» ──

async def _mydata_fields(monkeypatch, profile: dict) -> dict:
    from clients.gateway_mcp import gateway_mcp
    from states.utilities.mydata import MyDataState

    monkeypatch.setattr(gateway_mcp, "is_connected", lambda chat_id: True)
    monkeypatch.setattr(gateway_mcp, "get_user_profile", AsyncMock(return_value=profile))
    return await MyDataState.__new__(MyDataState)._get_dt_profile(42)


async def test_mydata_has_no_stage_field_for_reformer(monkeypatch):
    fields = await _mydata_fields(monkeypatch, _profile(degree=REFORMER))

    assert fields["Степень квалификации"] == "Реформатор"
    assert "Ступень ученика" not in fields


async def test_mydata_keeps_stage_field_for_student(monkeypatch):
    fields = await _mydata_fields(monkeypatch, _profile(degree=STUDENT))

    assert fields["Ступень ученика"] == "Проактивный (5/5)"


# ── Settings → digital twin ──

async def _settings_text(monkeypatch, profile: dict) -> str:
    from clients.gateway_mcp import gateway_mcp
    from states.common.settings import SettingsState

    monkeypatch.setattr(gateway_mcp, "is_connected", lambda chat_id: True)
    monkeypatch.setattr(gateway_mcp, "get_user_profile", AsyncMock(return_value=profile))
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()))
    await SettingsState.__new__(SettingsState)._handle_twin_connection({"chat_id": 42, "language": "ru"}, callback)
    return callback.message.edit_text.await_args.args[0]


async def test_settings_twin_hides_stage_for_reformer(monkeypatch):
    text = await _settings_text(monkeypatch, _profile(degree=REFORMER))

    assert "*Реформатор*" in text
    assert "📊" not in text


async def test_settings_twin_keeps_stage_for_student(monkeypatch):
    text = await _settings_text(monkeypatch, _profile(degree=STUDENT))

    assert "📊" in text and "*5*" in text


# ── /progress → «Диагностический профиль» ──

_CP = {"stage": 5, "bottleneck_slot": "cp.wld", "recommended_stream": "РР", "cp_scores": {"cp.rhy": 4}}


async def _cp_profile_text(degree) -> str:
    from states.utilities.progress import ProgressState

    state = ProgressState.__new__(ProgressState)
    state._show_section = AsyncMock()
    await state._show_cp_profile(MagicMock(), {"cp_profile": _CP, "degree": degree}, "ru")
    return state._show_section.await_args.args[1]


async def test_cp_profile_shows_qualification_for_reformer():
    text = await _cp_profile_text(REFORMER)

    assert "🎓 Квалификация: <b>Реформатор</b>" in text
    assert "Ступень" not in text
    assert "Узкое место" in text and "Профиль по слотам" in text   # the rest of the diagnostic stays


async def test_cp_profile_keeps_stage_for_student_and_unknown():
    assert "🎯 Ступень: <b>5 — Проактивный</b>" in await _cp_profile_text(STUDENT)
    assert "🎯 Ступень: <b>5 — Проактивный</b>" in await _cp_profile_text(None)


# ── /diagnose result ──

_DIAG = {"stage": 5, "bottleneck_slot": None, "recommended_stream": "РР"}


def test_diagnose_result_shows_qualification_instead_of_stage():
    from handlers.diagnose import _format_result

    text = _format_result(_DIAG, None, "Реформатор")

    assert "🎓 Квалификация: <b>Реформатор</b>" in text
    assert "Ступень" not in text and "ступени" not in text


def test_diagnose_result_keeps_stage_without_qualification():
    from handlers.diagnose import _format_result

    assert "Ступень: <b>Проактивный (5 из 5)</b>" in _format_result(_DIAG, None)


async def test_diagnose_qualification_read_is_best_effort(monkeypatch):
    from clients.gateway_mcp import gateway_mcp
    from handlers.diagnose import _qualification

    monkeypatch.setattr(gateway_mcp, "is_connected", lambda chat_id: True)
    monkeypatch.setattr(gateway_mcp, "dt_read_degree", AsyncMock(return_value=REFORMER))
    assert await _qualification(42) == "Реформатор"

    monkeypatch.setattr(gateway_mcp, "dt_read_degree", AsyncMock(return_value=STUDENT))
    assert await _qualification(42) is None

    monkeypatch.setattr(gateway_mcp, "dt_read_degree", AsyncMock(side_effect=TimeoutError("down")))
    assert await _qualification(42) is None

    reads = AsyncMock(return_value=REFORMER)
    monkeypatch.setattr(gateway_mcp, "is_connected", lambda chat_id: False)
    monkeypatch.setattr(gateway_mcp, "dt_read_degree", reads)
    assert await _qualification(42) is None
    reads.assert_not_awaited()                     # not connected: no gateway call


# ── Slot summary: «Цель на ступени N» only inside «Ученик» ──

async def _slot_stage(monkeypatch, degree=None, lms_degree=None):
    import json
    from handlers import slot

    row = {"stage": "5",
           "degree": json.dumps(degree) if degree else None,
           "lms_degree": json.dumps(lms_degree) if lms_degree else None}
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=row))

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("db.queries.get_intern", AsyncMock(return_value={"user_uuid": "u-1"}))
    monkeypatch.setattr("db.connection.get_pool", AsyncMock(return_value=SimpleNamespace(acquire=_Acquire)))
    return await slot._get_user_stage(42)


async def test_slot_stage_is_none_for_reformer(monkeypatch):
    assert await _slot_stage(monkeypatch, degree=REFORMER) is None
    assert await _slot_stage(monkeypatch, lms_degree=REFORMER) is None   # before the night run


async def test_slot_stage_kept_for_student_and_unknown(monkeypatch):
    assert await _slot_stage(monkeypatch, degree=STUDENT) == 5
    assert await _slot_stage(monkeypatch) == 5
