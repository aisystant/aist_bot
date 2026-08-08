"""
WP-406 Ф18: onboarding_completed логируется только когда закрыты ОБА разрыва (Х2+Х3).

Ранее событие лилось при регистрации (до того, как Х2/Х3 вообще начинались) —
переименовано в registration_completed. Новое onboarding_completed логируется
ровно один раз, тем путём, который закрывает второй из двух разрывов.

4 пути:
  test_x3_closes_second_after_x2   Х2 уже закрыт -> Х3 логирует onboarding_completed
  test_x3_closes_first_no_event    Х2 ещё не закрыт -> Х3 не логирует onboarding_completed
  test_x2_closes_second_after_x3   Х3 уже закрыт -> Х2 логирует onboarding_completed
  test_x2_closes_first_no_event    Х3 ещё не закрыт -> Х2 не логирует onboarding_completed

Backlog-guard (найдено 2026-07-11 живым дублем в проде, user_id=801926288 —
три onboarding_completed за 11 секунд от повторного тапа):
  test_x3_confirm_double_tap_no_duplicate_event   Х3 уже закрыт -> повторный тап молчит
  test_x2_finish_double_click_no_duplicate_event  Х2 уже закрыт -> повторный финиш молчит

Контракт mark-and-read (review High, peer-session 2026-08-08-01): mark_x2_done /
mark_x3_done атомарно ставят отметку и возвращают
{"newly_marked": bool, "x2_completed_at": dt|None, "x3_completed_at": dt|None} —
решение «логировать ли события» принимается по возврату mark, не по отдельному
get_status (гонка «neither fires»).
"""

import uuid
from datetime import datetime

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

_DT = datetime(2026, 8, 8, 12, 0, 0)


def _mock_callback(data: str, chat_id: int = 317106357):
    callback = AsyncMock()
    callback.data = data
    callback.from_user = MagicMock(id=chat_id)
    callback.message = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_x3_closes_second_after_x2():
    """Х2 уже закрыт до вызова -> подтверждение Х3 логирует onboarding_completed."""
    callback = _mock_callback("x3_confirm:marathon:0")
    account_id = uuid.uuid4()

    with patch("core.onboarder.storage.mark_x3_done", new_callable=AsyncMock,
               return_value={"newly_marked": True,
                             "x2_completed_at": _DT, "x3_completed_at": _DT}), \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("handlers.onboarding.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log:
        from handlers.onboarding import on_x3_confirm
        await on_x3_confirm(callback)

    logged_events = [call.args[1] for call in mock_log.await_args_list]
    assert "onboarding_completed" in logged_events
    completed_call = next(c for c in mock_log.await_args_list if c.args[1] == "onboarding_completed")
    assert completed_call.args[2]["closed_by"] == "x3"


@pytest.mark.asyncio
async def test_x3_closes_first_no_event():
    """Х2 ещё не закрыт -> подтверждение Х3 НЕ логирует onboarding_completed."""
    callback = _mock_callback("x3_confirm:marathon:0")
    account_id = uuid.uuid4()

    with patch("core.onboarder.storage.mark_x3_done", new_callable=AsyncMock,
               return_value={"newly_marked": True,
                             "x2_completed_at": None, "x3_completed_at": _DT}), \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("handlers.onboarding.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log:
        from handlers.onboarding import on_x3_confirm
        await on_x3_confirm(callback)

    logged_events = [call.args[1] for call in mock_log.await_args_list]
    assert "onboarding_completed" not in logged_events
    assert "x3_completed" in logged_events


@pytest.mark.asyncio
async def test_x2_closes_second_after_x3():
    """Х3 уже закрыт до вызова -> подтверждение последнего пункта Х2 логирует onboarding_completed."""
    bot = AsyncMock()
    chat_id = 317106357

    with patch("core.onboarder.storage.mark_x2_done", new_callable=AsyncMock,
               return_value={"newly_marked": True,
                             "x2_completed_at": _DT, "x3_completed_at": _DT}), \
         patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": True, "x3_done": True}), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("db.queries.users.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log, \
         patch("aiogram.types.InlineKeyboardMarkup"), \
         patch("aiogram.types.InlineKeyboardButton"), \
         patch("core.onboarder.x2._show_topic", new_callable=AsyncMock):
        from core.onboarder.x2 import _finish_x2
        await _finish_x2(bot, chat_id)

    logged_events = [call.args[1] for call in mock_log.await_args_list]
    assert "onboarding_completed" in logged_events
    completed_call = next(c for c in mock_log.await_args_list if c.args[1] == "onboarding_completed")
    assert completed_call.args[2]["closed_by"] == "x2"


@pytest.mark.asyncio
async def test_x2_closes_first_no_event():
    """Х3 ещё не закрыт -> подтверждение последнего пункта Х2 НЕ логирует onboarding_completed."""
    bot = AsyncMock()
    chat_id = 317106357

    with patch("core.onboarder.storage.mark_x2_done", new_callable=AsyncMock,
               return_value={"newly_marked": True,
                             "x2_completed_at": _DT, "x3_completed_at": None}), \
         patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": True, "x3_done": False}), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("db.queries.users.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log, \
         patch("aiogram.types.InlineKeyboardMarkup"), \
         patch("aiogram.types.InlineKeyboardButton"), \
         patch("core.onboarder.x2._show_topic", new_callable=AsyncMock):
        from core.onboarder.x2 import _finish_x2
        await _finish_x2(bot, chat_id)

    logged_events = [call.args[1] for call in mock_log.await_args_list]
    assert "onboarding_completed" not in logged_events
    assert "x2_completed" in logged_events


@pytest.mark.asyncio
async def test_x3_confirm_double_tap_no_duplicate_event():
    """Х3 уже закрыт (повторный тап по устаревшей кнопке) -> mark вернул
    newly_marked=False -> ни x3_completed, ни onboarding_completed не логируются
    повторно (WP-406 Ф18 backlog-guard, атомарный контракт review High)."""
    callback = _mock_callback("x3_confirm:marathon:0")

    with patch("core.onboarder.storage.mark_x3_done", new_callable=AsyncMock,
               return_value={"newly_marked": False,
                             "x2_completed_at": _DT, "x3_completed_at": _DT}) as mock_mark, \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("handlers.onboarding.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log:
        from handlers.onboarding import on_x3_confirm
        await on_x3_confirm(callback)

    mock_mark.assert_awaited_once()
    mock_log.assert_not_awaited()
    callback.message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_x2_finish_double_click_no_duplicate_event():
    """Х2 уже закрыт (повторный клик «Понятно» последнего пункта) -> mark вернул
    newly_marked=False -> ни x2_completed, ни onboarding_completed не логируются,
    поздравление не дублируется (атомарный контракт review High)."""
    bot = AsyncMock()
    chat_id = 317106357

    with patch("core.onboarder.storage.mark_x2_done", new_callable=AsyncMock,
               return_value={"newly_marked": False,
                             "x2_completed_at": _DT, "x3_completed_at": _DT}) as mock_mark, \
         patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": True, "x3_done": True}), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("db.queries.users.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log:
        from core.onboarder.x2 import _finish_x2
        await _finish_x2(bot, chat_id)

    mock_mark.assert_awaited_once()
    mock_log.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_x3_confirm_no_user_state_row_no_events():
    """mark вернул None (строки user_state нет) -> guard срабатывает, событий нет."""
    callback = _mock_callback("x3_confirm:marathon:0")

    with patch("core.onboarder.storage.mark_x3_done", new_callable=AsyncMock,
               return_value=None), \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("handlers.onboarding.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log:
        from handlers.onboarding import on_x3_confirm
        await on_x3_confirm(callback)

    mock_log.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
