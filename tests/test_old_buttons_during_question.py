"""WP-262 F4 (pilot decision 21.11, option A): old buttons during a "?" conversation.

After a "?" question the person stays in the consultation for a while. Buttons of
screens such as Progress reached a state without handle_callback and were lost
silently. Now a button that a screen declares as its own ends the conversation,
returns quietly to that screen and is handled there as usual. The screen is found
by the button itself, so it works after a bot restart and whatever screen the
question was asked from. Any other button keeps the conversation as before.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from core.machine import StateMachine  # noqa: E402
from states.common.plans import PlansState  # noqa: E402
from states.utilities.mydata import MyDataState  # noqa: E402
from states.utilities.progress import ProgressState  # noqa: E402

TRANSITIONS = REPO_ROOT / "config" / "transitions.yaml"
CHAT_ID = 7263
CONSULTATION = "common.consultation"
PROGRESS = "utility.progress"
MENU = "common.mode_select"


def consultation_state():
    # The real consultation has no handle_callback; a SimpleNamespace has none either.
    return SimpleNamespace(
        name=CONSULTATION,
        exit=AsyncMock(return_value={"consultation_complete": True}),
        enter=AsyncMock(),
    )


def screen_state(name, prefixes=(), event=None):
    return SimpleNamespace(
        name=name,
        callback_prefixes=prefixes,
        exit=AsyncMock(return_value={}),
        enter=AsyncMock(),
        handle_callback=AsyncMock(return_value=event),
    )


def machine_with(*states) -> StateMachine:
    machine = StateMachine()
    machine.load_transitions(TRANSITIONS)
    for state in states:
        machine._states[state.name] = state
    return machine


def user_in(state_name: str) -> dict:
    return {"chat_id": CHAT_ID, "current_state": state_name, "language": "ru", "current_context": {}}


@pytest.fixture
def db():
    with patch("db.queries.update_user_state", new=AsyncMock()) as update_state, \
         patch("db.queries.get_intern", new=AsyncMock(return_value=user_in(PROGRESS))):
        yield update_state


@pytest.mark.asyncio
async def test_screen_button_ends_conversation_and_is_handled_by_its_screen(db):
    # Nothing remembered about where "?" was asked: the case after a bot restart.
    consultation, progress = consultation_state(), screen_state(PROGRESS, ("progress_",))
    machine = machine_with(consultation, progress)
    callback = SimpleNamespace(data="progress_calendar")

    await machine.handle_callback(user_in(CONSULTATION), callback)

    consultation.exit.assert_awaited_once()            # the AI conversation is ended
    db.assert_awaited_once_with(CHAT_ID, PROGRESS)      # the person is back on Progress
    progress.enter.assert_not_awaited()                 # quietly: the old screen is still in the chat
    progress.handle_callback.assert_awaited_once()
    assert progress.handle_callback.call_args.args[1] is callback


@pytest.mark.asyncio
async def test_button_goes_to_its_own_screen_not_to_where_the_question_was_asked(db):
    consultation = consultation_state()
    progress, menu = screen_state(PROGRESS, ("progress_",)), screen_state(MENU)
    machine = machine_with(consultation, progress, menu)
    machine.set_previous_state(CHAT_ID, MENU)  # "?" was asked from the main menu

    await machine.handle_callback(user_in(CONSULTATION), SimpleNamespace(data="progress_calendar"))

    progress.handle_callback.assert_awaited_once()
    menu.handle_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_pressed_during_conversation_leads_to_main_menu(db):
    consultation = consultation_state()
    progress, menu = screen_state(PROGRESS, ("progress_",), event="back"), screen_state(MENU)
    machine = machine_with(consultation, progress, menu)

    await machine.handle_callback(user_in(CONSULTATION), SimpleNamespace(data="progress_exit"))

    progress.handle_callback.assert_awaited_once()
    menu.enter.assert_awaited_once()
    assert db.call_args_list[-1].args == (CHAT_ID, MENU)


@pytest.mark.asyncio
async def test_button_of_undeclared_screen_keeps_the_conversation(db):
    consultation, progress = consultation_state(), screen_state(PROGRESS, ("progress_",))
    machine = machine_with(consultation, progress)
    machine.set_previous_state(CHAT_ID, PROGRESS)

    await machine.handle_callback(user_in(CONSULTATION), SimpleNamespace(data="lang_ru"))

    consultation.exit.assert_not_awaited()
    progress.handle_callback.assert_not_awaited()
    db.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_modal_states_hand_buttons_over(db):
    # A non-modal state without handle_callback (marathon screens get their buttons
    # elsewhere) must not be left because of a screen button.
    marathon = SimpleNamespace(name="workshop.marathon.lesson", exit=AsyncMock(return_value={}), enter=AsyncMock())
    progress = screen_state(PROGRESS, ("progress_",))
    machine = machine_with(marathon, progress)

    await machine.handle_callback(user_in("workshop.marathon.lesson"), SimpleNamespace(data="progress_exit"))

    marathon.exit.assert_not_awaited()
    progress.handle_callback.assert_not_awaited()
    db.assert_not_awaited()


def test_screens_with_filtered_button_routers_declare_their_prefixes():
    # These screens get their buttons through routers that require the screen to be
    # current (handlers/callbacks.py), so during a conversation the press falls through.
    assert ProgressState.callback_prefixes == ("progress_",)
    assert PlansState.callback_prefixes == ("plans_",)
    # My data is left out on purpose: its old delete and reset confirmations must not
    # start working from a stale message during a conversation.
    assert MyDataState.callback_prefixes == ()
