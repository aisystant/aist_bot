"""Unit-тесты has_active_subscription (WP-7 Ф133): фоллбэк на таблицу
subscription.contract, когда Aisystant API говорит "нет" или падает.

WP-7 Ф133 (09.09.2026): фикс подписки на проде — T3/T4-пользователи видели T1,
потому что прямой вызов clients.aisystant.has_active_subscription() не знал
про подписки, живущие только в subscription.contract (легаси-миграция LMS).
tests/ по этому коду был пуст до этого теста (отмечено в WP-7.md 03.10, 07.10).
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from core import tier_detector  # noqa: E402

CHAT_ID = 4242
AISYSTANT_ID = "aisystant-user-4242"


@pytest.mark.asyncio
async def test_aisystant_active_skips_contract_check():
    """Aisystant говорит "да" -- возвращает True, таблицу contract не трогает."""
    with patch("clients.aisystant.aisystant.has_active_subscription", new=AsyncMock(return_value=True)), \
         patch.object(tier_detector, "_check_contract_subscription", new=AsyncMock(return_value=False)) as contract_check:
        result = await tier_detector.has_active_subscription(CHAT_ID, AISYSTANT_ID)
    assert result is True
    contract_check.assert_not_called()


@pytest.mark.asyncio
async def test_aisystant_false_falls_through_to_contract():
    """Aisystant говорит "нет", но запись есть в contract -- всё равно True (сам баг WP-7 Ф133)."""
    with patch("clients.aisystant.aisystant.has_active_subscription", new=AsyncMock(return_value=False)), \
         patch.object(tier_detector, "_check_contract_subscription", new=AsyncMock(return_value=True)) as contract_check:
        result = await tier_detector.has_active_subscription(CHAT_ID, AISYSTANT_ID)
    assert result is True
    contract_check.assert_awaited_once_with(CHAT_ID)


@pytest.mark.asyncio
async def test_aisystant_exception_falls_through_to_contract():
    """Aisystant API упал -- не считается "нет подписки", проверяется contract."""
    with patch("clients.aisystant.aisystant.has_active_subscription", new=AsyncMock(side_effect=RuntimeError("timeout"))), \
         patch.object(tier_detector, "_check_contract_subscription", new=AsyncMock(return_value=True)) as contract_check:
        result = await tier_detector.has_active_subscription(CHAT_ID, AISYSTANT_ID)
    assert result is True
    contract_check.assert_awaited_once_with(CHAT_ID)


@pytest.mark.asyncio
async def test_both_sources_say_no_returns_false():
    """Ни Aisystant, ни contract не видят подписку -- честный False, не угадывание."""
    with patch("clients.aisystant.aisystant.has_active_subscription", new=AsyncMock(return_value=False)), \
         patch.object(tier_detector, "_check_contract_subscription", new=AsyncMock(return_value=False)):
        result = await tier_detector.has_active_subscription(CHAT_ID, AISYSTANT_ID)
    assert result is False
