"""РП330: живая выдача /learn обязана гасить scheduler-рассылку того же дня.

Регрессия (2026-08-10, живые аккаунты): _deliver_marathon_lesson никогда не
трогал learning.marathon_queue, поэтому _process_marathon_queue не знал о
живой доставке и присылал тот же День N повторно по расписанию на следующее утро.
"""

from unittest.mock import AsyncMock, patch

import pytest

from handlers.marathon import _deliver_marathon_lesson


@pytest.mark.asyncio
async def test_live_delivery_marks_queue_row_sent():
    """Успешная живая доставка занятия помечает очередь этого дня отправленной."""
    target = AsyncMock()

    with patch("core.marathon_content.get_day_text", return_value="Занятие дня 3"), \
         patch("handlers.marathon.mark_lesson_practice_delivered", new=AsyncMock()) as mock_mark:
        await _deliver_marathon_lesson(user_id=95638145, target=target, day=3, intern={"language": "ru"})

    target.answer.assert_awaited_once()
    mock_mark.assert_awaited_once_with(95638145, 3)


@pytest.mark.asyncio
async def test_live_delivery_failure_does_not_block_lesson_send():
    """Сбой отметки очереди — best-effort, не должен ронять уже отправленный ответ."""
    target = AsyncMock()

    with patch("core.marathon_content.get_day_text", return_value="Занятие дня 1"), \
         patch("handlers.marathon.mark_lesson_practice_delivered",
                new=AsyncMock(side_effect=RuntimeError("db down"))):
        await _deliver_marathon_lesson(user_id=1, target=target, day=1, intern={"language": "ru"})

    target.answer.assert_awaited_once()
