"""
WP-406 (пир-сессия 2026-08-13-01): показ «Освоиться» перенесён из Экрана B
(onboarding.py, параллельно с согласием) в on_consent_accept/on_consent_decline
(consent.py) — после того, как пользователь ответил на согласие.

4 merge-blockers:
  test_accept_gap_open_offers_onboarder     (разрыв Х2/Х3 открыт -> «Освоиться», не intent-экран)
  test_accept_gap_closed_shows_intent       (разрыв закрыт -> intent-экран, не «Освоиться»)
  test_decline_gap_open_offers_onboarder    (отказ + открытый разрыв -> «Освоиться»)
  test_decline_gap_closed_no_offer          (отказ + закрытый разрыв -> ничего лишнего)
"""

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_callback(user_id: int = 317106357) -> MagicMock:
    callback = MagicMock()
    callback.from_user = MagicMock(id=user_id)
    callback.answer = AsyncMock()
    callback.message = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


def _gap_status(open_gap: bool) -> dict:
    return {"x2_done": not open_gap, "x3_done": not open_gap}


@pytest.mark.asyncio
async def test_accept_gap_open_offers_onboarder():
    """has_onboarder_gap=True -> _maybe_offer_onboarder вызван, intent-экран — нет."""
    callback = _mock_callback()
    account_id = str(uuid.uuid4())
    intern = {"current_context": {}}

    with patch("handlers.consent._resolve_account", new_callable=AsyncMock,
               return_value=(intern, account_id)), \
         patch("clients.gateway_mcp.gateway_mcp.grant_consent", new_callable=AsyncMock,
               return_value={"success": True}), \
         patch("handlers.consent.set_consent_grant", new_callable=AsyncMock), \
         patch("handlers.consent.get_consent", new_callable=AsyncMock, return_value=None), \
         patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value=_gap_status(open_gap=True)), \
         patch("handlers.onboarding._maybe_offer_onboarder", new_callable=AsyncMock) as mock_offer, \
         patch("handlers.consent._send_intent_screen", new_callable=AsyncMock) as mock_intent:
        from handlers.consent import on_consent_accept
        await on_consent_accept(callback)

    mock_offer.assert_called_once_with(callback.message, callback.from_user.id, bypass_cooldown=True)
    mock_intent.assert_not_called()


@pytest.mark.asyncio
async def test_accept_gap_closed_shows_intent():
    """has_onboarder_gap=False -> intent-экран вызван, _maybe_offer_onboarder — нет."""
    callback = _mock_callback()
    account_id = str(uuid.uuid4())
    intern = {"current_context": {}}

    with patch("handlers.consent._resolve_account", new_callable=AsyncMock,
               return_value=(intern, account_id)), \
         patch("clients.gateway_mcp.gateway_mcp.grant_consent", new_callable=AsyncMock,
               return_value={"success": True}), \
         patch("handlers.consent.set_consent_grant", new_callable=AsyncMock), \
         patch("handlers.consent.get_consent", new_callable=AsyncMock, return_value=None), \
         patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value=_gap_status(open_gap=False)), \
         patch("handlers.onboarding._maybe_offer_onboarder", new_callable=AsyncMock) as mock_offer, \
         patch("handlers.consent._send_intent_screen", new_callable=AsyncMock) as mock_intent:
        from handlers.consent import on_consent_accept
        await on_consent_accept(callback)

    mock_intent.assert_called_once_with(callback.message)
    mock_offer.assert_not_called()


@pytest.mark.asyncio
async def test_decline_gap_open_offers_onboarder():
    """Отказ от согласия + открытый разрыв Х2/Х3 -> реальная кнопка «Освоиться», не текстовый hint."""
    callback = _mock_callback()

    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value=_gap_status(open_gap=True)), \
         patch("handlers.onboarding._maybe_offer_onboarder", new_callable=AsyncMock) as mock_offer:
        from handlers.consent import on_consent_decline
        await on_consent_decline(callback)

    mock_offer.assert_called_once_with(callback.message, callback.from_user.id, bypass_cooldown=True)
    edit_text_args = callback.message.edit_text.call_args
    assert "Освоиться" not in edit_text_args.args[0]


@pytest.mark.asyncio
async def test_decline_gap_closed_no_offer():
    """Отказ от согласия + закрытый разрыв Х2/Х3 -> «Освоиться» не показывается."""
    callback = _mock_callback()

    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value=_gap_status(open_gap=False)), \
         patch("handlers.onboarding._maybe_offer_onboarder", new_callable=AsyncMock) as mock_offer:
        from handlers.consent import on_consent_decline
        await on_consent_decline(callback)

    mock_offer.assert_not_called()
