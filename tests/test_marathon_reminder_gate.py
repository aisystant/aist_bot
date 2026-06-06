"""WP-330 Block MAR — гейт cutover для legacy-напоминаний.

send_reminder обязан гасить legacy +1h/+3h напоминания для пользователей,
переведённых на новый движок марафона (есть строка в learning.marathon_progress).

Регрессия (6 июня): после cutover legacy-доставка была отключена, а напоминания
продолжали лететь («День 1 ещё не начат» при пройденном «Дне 2») — два движка
работали параллельно.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from core.scheduler import send_reminder  # noqa: E402


@pytest.mark.asyncio
async def test_reminder_suppressed_for_newcomer_marathon():
    """Пользователь на новом движке → legacy-напоминание погашено до любой отправки."""
    bot = AsyncMock()
    with patch("core.scheduler.get_intern", new=AsyncMock(return_value={"language": "ru"})), \
         patch("core.scheduler.get_topics_today") as mock_topics, \
         patch("db.queries.marathon_newcomer.is_on_newcomer_marathon",
               new=AsyncMock(return_value=True)):
        await send_reminder(12345, "+1h", bot)

    bot.send_message.assert_not_called()
    # Гейт стоит ДО проверки topics_today — короткое замыкание.
    mock_topics.assert_not_called()


@pytest.mark.asyncio
async def test_reminder_flows_through_for_legacy_user():
    """Legacy-пользователь (нет строки в marathon_progress) проходит гейт дальше."""
    bot = AsyncMock()
    with patch("core.scheduler.get_intern", new=AsyncMock(return_value={"language": "ru"})), \
         patch("core.scheduler.get_topics_today", return_value=1) as mock_topics, \
         patch("db.queries.marathon_newcomer.is_on_newcomer_marathon",
               new=AsyncMock(return_value=False)) as mock_gate:
        await send_reminder(12345, "+1h", bot)

    mock_gate.assert_awaited_once()
    mock_topics.assert_called_once()       # legacy-пользователь прошёл гейт
    bot.send_message.assert_not_called()   # но остановлен на «topics_today > 0»
