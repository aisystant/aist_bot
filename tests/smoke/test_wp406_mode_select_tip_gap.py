"""
WP-406 (живая находка 13.08): ротационный tip mode_select («Попробуй Ленту» и
т.п.) уводит в сторону от незакрытого онбординга — сразу вслед идёт оффер
«Освоиться». Пока разрыв Х2/Х3 открыт, tip не должен показываться.

2 merge-blockers:
  test_tip_suppressed_when_gap_open    (разрыв открыт -> пустой tip)
  test_tip_shown_when_gap_closed       (разрыв закрыт -> непустой tip из пула тира)
"""

import pytest
from unittest.mock import AsyncMock, patch

from states.common.mode_select import _get_random_tip
from core.tier_config import UITier


@pytest.mark.asyncio
async def test_tip_suppressed_when_gap_open():
    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": False, "x3_done": False}):
        tip = await _get_random_tip(UITier.T1, "ru", 317106357)

    assert tip == ""


@pytest.mark.asyncio
async def test_tip_shown_when_gap_closed():
    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": True, "x3_done": True}):
        tip = await _get_random_tip(UITier.T1, "ru", 317106357)

    assert tip != ""
    assert "welcome.tips." not in tip  # реальный текст, не сырой i18n-ключ
