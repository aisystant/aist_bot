"""WP-262 F4 stage 1: no screen drops a person's text silently.

Before: in Progress both plain text and "?" questions were dropped (the routing
table had no allow_global for utility.progress, while the class declared it);
in training setup plain text was dropped. After asking "?" from a screen and
returning, that screen's own "back: _previous" pointed at itself (dead button).
"""

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from core.machine import StateMachine  # noqa: E402
from i18n import t  # noqa: E402
from states.training.setup import TrainingSetupState  # noqa: E402
from states.utilities.progress import ProgressState  # noqa: E402

TRANSITIONS = REPO_ROOT / "config" / "transitions.yaml"
CONSULTATION = "common.consultation"
CHAT_ID = 7262


def make(state_cls):
    state = state_cls(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
    state.send = AsyncMock()
    return state


def machine_with(*state_names: str) -> StateMachine:
    machine = StateMachine()
    machine.load_transitions(TRANSITIONS)
    for name in state_names:
        machine._states[name] = SimpleNamespace(name=name)
    return machine


def user(state: str = "utility.progress") -> dict:
    return {"chat_id": CHAT_ID, "current_state": state, "language": "ru", "current_context": {}}


# --- routing: "?" from Progress reaches the agent ---------------------------

def test_question_from_progress_goes_to_consultation():
    machine = machine_with(CONSULTATION)

    assert machine.check_global_event("?что дальше", "utility.progress") == CONSULTATION


def test_question_in_training_setup_stays_disabled_by_design():
    machine = machine_with(CONSULTATION)

    assert machine.check_global_event("?что дальше", "training.setup") is None


# --- hints instead of silence ------------------------------------------------

@pytest.mark.asyncio
async def test_plain_text_in_progress_gets_question_hint_once():
    state = make(ProgressState)

    result = await state.handle(user(), SimpleNamespace(text="как мои дела"))

    assert result is None
    state.send.assert_awaited_once()
    assert state.send.call_args.args[1] == t("progress.question_hint", "ru")
    assert "«?»" in state.send.call_args.args[1]


@pytest.mark.asyncio
async def test_plain_text_in_training_setup_points_to_buttons_not_question_mark():
    state = make(TrainingSetupState)

    result = await state.handle(user("training.setup"), SimpleNamespace(text="хочу по порядку"))

    assert result is None
    state.send.assert_awaited_once()
    sent = state.send.call_args.args[1]
    assert sent == t("training.setup_use_buttons", "ru")
    assert "?" not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("state_cls", [ProgressState, TrainingSetupState])
@pytest.mark.parametrize("text", [None, "", "   ", "/unknown_command"])
async def test_no_hint_for_non_text_empty_or_command(state_cls, text):
    state = make(state_cls)

    result = await state.handle(user(), SimpleNamespace(text=text))

    assert result is None
    state.send.assert_not_awaited()


@pytest.mark.parametrize("key", ["progress.question_hint", "training.setup_use_buttons"])
def test_hint_keys_are_translated_in_every_language(key):
    assert t(key, "ru") != key
    # t() falls back to Russian for a missing translation, so compare against it.
    for lang in ("en", "es", "fr", "zh"):
        assert t(key, lang) != t(key, "ru"), f"{key} not translated to {lang}"


# --- back after a "?" round trip is not a dead button --------------------------

def test_previous_pointing_at_current_state_falls_back_to_menu():
    machine = machine_with()
    machine.set_previous_state(CHAT_ID, "utility.progress")  # set when "?" was asked from Progress

    assert machine.get_next_state("utility.progress", "back", CHAT_ID) == "common.mode_select"


def test_previous_pointing_elsewhere_is_kept():
    machine = machine_with()
    machine.set_previous_state(CHAT_ID, "feed.digest")

    assert machine.get_next_state("utility.progress", "back", CHAT_ID) == "feed.digest"


# --- guard: an event a class declares is never missing from the routing table ---
# One direction only: a class declaring less than the table is harmless at runtime
# (the machine reads only transitions.yaml); a class declaring more silently
# disables "?" on that screen, which is the Progress bug.

def _declared_allow_global() -> dict[str, list[str]]:
    """State name -> class-level allow_global, read statically from states/ (no imports)."""
    declared = {}
    for path in (REPO_ROOT / "states").rglob("*.py"):
        if "archive" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            attrs = {
                stmt.targets[0].id: stmt.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id in ("name", "allow_global")
            }
            if "name" in attrs and "allow_global" in attrs:
                declared[ast.literal_eval(attrs["name"])] = ast.literal_eval(attrs["allow_global"])
    return declared


def test_class_allow_global_is_present_in_routing_table():
    # The machine reads allow_global only from transitions.yaml; a class attribute
    # that the table lacks silently disables "?" on that screen.
    table = yaml.safe_load(TRANSITIONS.read_text(encoding="utf-8"))["states"]
    declared = _declared_allow_global()
    assert "utility.progress" in declared  # the scan really sees state classes

    drift = {}
    for name, events in declared.items():
        if name not in table:
            continue
        missing = set(events) - set((table[name] or {}).get("allow_global") or [])
        if missing:
            drift[name] = sorted(missing)
    assert drift == {}
