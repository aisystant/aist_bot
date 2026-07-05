"""
Smoke test for SafeBot Markdown → HTML conversion at transport layer.

Uses a mock aiogram session to observe the real request that SafeBot sends,
without hitting Telegram API.
"""

from unittest.mock import MagicMock

import pytest
from aiogram.client.session.base import BaseSession

from core.safe_bot import SafeBot


class _CaptureSession(BaseSession):
    """Fake session that records the last Telegram method and returns a dummy Message."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return MagicMock()

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError("not used")


@pytest.mark.asyncio
async def test_safe_bot_normalizes_em_dash_and_converts_bold():
    """
    Input with double spaces around em-dash and Markdown bold becomes clean HTML.
    Verifies the full transport-layer pipeline: enforce_iwe_style + md_to_html.
    """
    session = _CaptureSession()
    bot = SafeBot(token="123456:dummy-token", session=session)

    # "Пилот  —  это" has double spaces around the em-dash; allowed pattern
    # should normalize to single spaces. Markdown bold elsewhere is converted to HTML.
    await bot.send_message(
        chat_id=1,
        text="Пилот  —  это **bold** test",
        parse_mode="Markdown",
    )

    assert len(session.calls) == 1
    sent = session.calls[0]

    assert sent.text == "Пилот — это <b>bold</b> test"
    assert sent.parse_mode == "HTML"
    assert "**" not in sent.text
