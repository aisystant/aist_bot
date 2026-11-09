"""WP-5 (09.11.2026): invalid text input must leave settings input mode.

The pilot's account sat in settings waiting for a WakaTime key; every later
message, including a question, was parsed as a key and answered with
«Невалидный API-ключ». Now one invalid entry reports the error, adds a hint
and clears the waiting field, so the next message is handled normally.
"""

from unittest.mock import AsyncMock, patch

import pytest

from states.common.settings import SettingsState


def _user(waiting: str) -> dict:
    return {"chat_id": 1, "language": "ru", "current_context": {"settings_waiting_for": waiting}}


def _state() -> SettingsState:
    state = SettingsState.__new__(SettingsState)
    state.send = AsyncMock()
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("field,text", [
    ("wakatime_key", "что такое портной?"),
    ("schedule_marathon", "завтра утром"),
    ("schedule_feed", "25:99"),
])
async def test_invalid_input_clears_waiting_and_hints(field, text):
    state = _state()
    user = _user(field)

    with patch("states.common.settings.update_intern", new=AsyncMock()) as upd:
        await state._handle_text_input(user, field, text)

    assert user["current_context"]["settings_waiting_for"] is None
    upd.assert_awaited()  # waiting field persisted, not only cleared in memory
    sent = state.send.await_args.args[1]
    assert "Ввод отменён" in sent


@pytest.mark.asyncio
async def test_invalid_donate_amount_clears_waiting():
    state = _state()
    user = _user("donate_amount")

    with patch("states.common.settings.update_intern", new=AsyncMock()):
        await state._handle_text_input(user, "donate_amount", "много")

    assert user["current_context"]["settings_waiting_for"] is None
    assert "Ввод отменён" in state.send.await_args.args[1]


@pytest.mark.asyncio
async def test_valid_time_still_saved():
    state = _state()
    state.enter = AsyncMock()
    user = _user("schedule_marathon")

    with patch("states.common.settings.update_intern", new=AsyncMock()) as upd, \
         patch("db.queries.events.log_event", new=AsyncMock()):
        await state._handle_text_input(user, "schedule_marathon", "7:30")

    upd.assert_any_await(1, schedule_time="07:30")
    assert user["current_context"]["settings_waiting_for"] is None
