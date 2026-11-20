"""
Smoke test: retired Tailor buttons (WP-262 F3a, wave 0b).

Old "Answer"/"Skip" Tailor buttons still sit in user chats. After the entry is
closed a press must behave like any expired button: alert, buttons removed,
no state transition and no learning_completed event.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.smoke.conftest import make_intern


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["tailor_answer:SS.F1.01:1:2", "tailor_skip:SS.F1.01:1:2"])
async def test_retired_tailor_button_answers_expired_without_side_effects(data):
    from handlers.callbacks import cb_tailor_actions

    callback = MagicMock()
    callback.data = data
    callback.message.chat.id = 12345
    callback.from_user.id = 12345
    callback.answer = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    state = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.go_to = AsyncMock()

    with patch("handlers.callbacks.get_intern", new_callable=AsyncMock, return_value=make_intern()), \
         patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log_event:
        await cb_tailor_actions(callback, state)

    callback.answer.assert_awaited_once()
    assert callback.answer.call_args.kwargs.get("show_alert") is True
    callback.message.edit_reply_markup.assert_awaited_once()
    dispatcher.go_to.assert_not_called()
    mock_log_event.assert_not_called()
    state.clear.assert_not_called()
