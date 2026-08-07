"""
Tests for the /mydata_delete command (handlers/commands.py + states/utilities/mydata.py).

Source-inspection style, matching tests/test_delete_all_user_data_tables.py — no live
bot/DB fixtures required. Regression coverage for the 2026-08-07 finding: /privacy
promised a command Telegram cannot register (hyphen not allowed in command names).

Run: python3 -m pytest tests/test_mydata_delete_command.py -v
"""

import inspect
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.commands import cmd_mydata_delete
from states.utilities.mydata import MyDataState


def test_mydata_delete_command_registered_with_valid_telegram_name():
    """Telegram command names allow only [a-z0-9_] — a hyphen (as /privacy used to
    promise) is never parsed as a bot_command entity. inspect.getsource() includes
    the decorator line for a plain @decorator immediately above a def."""
    decorator_line = inspect.getsource(cmd_mydata_delete).splitlines()[0]
    assert decorator_line == '@commands_router.message(Command("mydata_delete"))'


def test_mydata_delete_routes_through_action_delete_context():
    """cmd_mydata_delete must delegate to the shared _route_to_mydata helper with
    context={'action': 'delete'} — not call a delete function directly — so it goes
    through the exact same SM entry point (MyDataState.enter) as the /mydata →
    button path, not a fast-path/duplicate to the confirm screen."""
    from handlers.commands import _route_to_mydata
    cmd_source = inspect.getsource(cmd_mydata_delete)
    assert "_route_to_mydata(message, state, context={'action': 'delete'})" in cmd_source
    helper_source = inspect.getsource(_route_to_mydata)
    assert "route_command('mydata'" in helper_source or 'route_command("mydata"' in helper_source


def test_enter_with_delete_action_calls_start_delete_flow_not_hub():
    """MyDataState.enter(context={'action': 'delete'}) must short-circuit to
    _start_delete_flow before building the hub menu — not show the hub with the
    delete button user has to find and tap again."""
    source = inspect.getsource(MyDataState.enter)
    action_check_pos = source.find("context.get('action')")
    start_delete_pos = source.find("_start_delete_flow")
    hub_text_pos = source.find("mydata.title")
    assert action_check_pos != -1, "enter() does not check context['action']"
    assert start_delete_pos != -1, "enter() does not call _start_delete_flow"
    assert start_delete_pos < hub_text_pos, (
        "_start_delete_flow must be called before the hub menu is built"
    )


def test_start_delete_flow_does_not_skip_to_final_execution():
    """Regression guard for the risk Kimi flagged in peer-session
    2026-08-07-05-wp476-mydata-delete-crash-fix: the command alias must land on
    the confirm-phrase screen (awaiting_delete=True), never call
    delete_all_user_data directly."""
    source = inspect.getsource(MyDataState.enter)
    assert "delete_all_user_data" not in source
    assert "_execute_delete" not in source
