"""WP-262 F3a: a saved state that is no longer registered must not leave the person stuck.

Retiring a screen (Tailor, tailor.response) leaves its name saved for people who were
on it. The machine used to handle the next update with the start state but under the
stale name, so no transition was found: no screen, the name stayed in the database and
every next message repeated the dead end. Now the default state is saved and the person
continues from it: a message reaches the menu through the start screen, an old button
opens the menu, and "back" never points at a state that is not registered.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from core.machine import MENU_STATE, StateMachine  # noqa: E402

TRANSITIONS = REPO_ROOT / "config" / "transitions.yaml"
CHAT_ID = 8114
RETIRED = "tailor.response"
START = "common.start"


def start_state():
    # The real start screen has no handle_callback; a SimpleNamespace has none either.
    return SimpleNamespace(
        name=START,
        handle=AsyncMock(return_value="existing_user"),
        exit=AsyncMock(return_value={}),
        enter=AsyncMock(),
    )


def menu_state():
    return SimpleNamespace(
        name=MENU_STATE,
        exit=AsyncMock(return_value={}),
        enter=AsyncMock(),
        handle_callback=AsyncMock(return_value=None),
    )


def machine_with(*states) -> StateMachine:
    machine = StateMachine()
    machine.load_transitions(TRANSITIONS)
    for state in states:
        machine._states[state.name] = state
    return machine


def user_in(state_name: str, name: str = "Анна") -> dict:
    return {"chat_id": CHAT_ID, "current_state": state_name, "language": "ru",
            "current_context": {}, "name": name}


@pytest.fixture
def db():
    with patch("db.queries.update_user_state", new=AsyncMock()) as update_state, \
         patch("db.queries.get_intern", new=AsyncMock(return_value=user_in(MENU_STATE))):
        yield update_state


def test_retired_tailor_state_is_not_registered_or_routed():
    machine = machine_with()
    assert RETIRED not in machine._transitions


@pytest.mark.asyncio
async def test_message_from_retired_state_reaches_the_menu(db):
    start, menu = start_state(), menu_state()
    machine = machine_with(start, menu)

    await machine.handle(user_in(RETIRED), SimpleNamespace(text="привет"))

    # The text is not read as an answer of the start screen; the person gets the menu.
    start.handle.assert_not_awaited()
    menu.enter.assert_awaited_once()
    assert db.await_args_list == [call(CHAT_ID, START), call(CHAT_ID, MENU_STATE)]


@pytest.mark.asyncio
async def test_message_from_retired_state_without_name_asks_for_the_name(db):
    start, menu = start_state(), menu_state()
    machine = machine_with(start, menu)

    await machine.handle(user_in(RETIRED, name=""), SimpleNamespace(text="привет"))

    # "привет" must not become the name: the start screen asks for it instead.
    start.handle.assert_not_awaited()
    start.enter.assert_awaited_once()
    menu.enter.assert_not_awaited()
    assert db.await_args_list == [call(CHAT_ID, START), call(CHAT_ID, START)]


@pytest.mark.asyncio
async def test_old_button_from_retired_state_opens_the_menu(db):
    start, menu = start_state(), menu_state()
    machine = machine_with(start, menu)

    callback = SimpleNamespace(data="tailor_answer:SS.F1.01:1:2", answer=AsyncMock())

    await machine.handle_callback(user_in(RETIRED), callback)

    callback.answer.assert_awaited_once()  # no endless spinner on the pressed button
    menu.enter.assert_awaited_once()
    menu.handle_callback.assert_not_awaited()
    assert db.await_args_list == [call(CHAT_ID, START), call(CHAT_ID, MENU_STATE)]


def test_back_never_returns_to_an_unregistered_state():
    machine = machine_with(menu_state())
    machine.set_previous_state(CHAT_ID, RETIRED)
    machine._transitions["utility.probe"] = {"events": {"back": "_previous"}}

    assert machine.get_next_state("utility.probe", "back", CHAT_ID) == MENU_STATE
