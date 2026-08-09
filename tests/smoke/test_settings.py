"""
Smoke-тесты: handlers/settings.py.

on_upd_mode — регрессия дрейфа mode_*-кнопок (карточка WP-262, найдено 27.07):
mode_router (mode_marathon/mode_feed/mode_training) не подключается при
USE_STATE_MACHINE=true (engines/integration.py), поэтому легаси cmd_mode()
рисовал кнопки без обработчиков. При активной SM хендлер обязан роутить
через dispatcher.route_command('mode', ...), не звать cmd_mode() напрямую.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.smoke.conftest import make_intern


def _fake_callback():
    callback = MagicMock()
    callback.message.chat.id = 12345
    callback.message.delete = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_upd_mode_routes_via_dispatcher_when_sm_active():
    """SM активна → upd_mode идёт через dispatcher.route_command('mode', ...), не через legacy cmd_mode()."""
    from handlers.settings import on_upd_mode

    callback = _fake_callback()
    state = AsyncMock()

    mock_dispatcher = MagicMock()
    mock_dispatcher.is_sm_active = True
    mock_dispatcher.route_command = AsyncMock(return_value=True)

    with patch("handlers.get_dispatcher", return_value=mock_dispatcher), \
         patch("handlers.settings.get_intern", new_callable=AsyncMock, return_value=make_intern()), \
         patch("engines.mode_selector.cmd_mode", new_callable=AsyncMock) as mock_cmd_mode:
        await on_upd_mode(callback, state)

    mock_dispatcher.route_command.assert_called_once()
    assert mock_dispatcher.route_command.call_args[0][0] == "mode"
    mock_cmd_mode.assert_not_called()
    callback.message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_upd_mode_falls_back_to_legacy_without_sm():
    """SM выключена (get_dispatcher() → None) → прежнее поведение: legacy cmd_mode()."""
    from handlers.settings import on_upd_mode

    callback = _fake_callback()
    state = AsyncMock()

    with patch("handlers.get_dispatcher", return_value=None), \
         patch("handlers.settings.get_intern", new_callable=AsyncMock, return_value=make_intern()), \
         patch("engines.mode_selector.cmd_mode", new_callable=AsyncMock) as mock_cmd_mode:
        await on_upd_mode(callback, state)

    mock_cmd_mode.assert_called_once_with(callback.message)
