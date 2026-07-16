"""
WP-406 Ф20: consent-gap detect в /me (T3+ без opt-in вне /link).

3 merge-blockers:
  test_consent_gap_no_consent_row      (нет строки consent -> экран согласия)
  test_consent_gap_opt_in_false        (opt_in=False -> экран согласия)
  test_consent_gap_opt_in_true_no_gate (opt_in=True -> gate не срабатывает)
"""

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_message():
    message = AsyncMock()
    message.chat = MagicMock(id=317106357)
    message.from_user = MagicMock(id=317106357)
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_consent_gap_no_consent_row():
    """T3+ пользователь без строки в tracking_consent видит экран согласия, не пустой дашборд."""
    message = _mock_message()
    account_id = uuid.uuid4()

    with patch("handlers.twin.get_intern", new_callable=AsyncMock, return_value=None), \
         patch("core.tier_detector.detect_ui_tier", new_callable=AsyncMock, return_value=3), \
         patch("helpers.dual_write.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value=str(account_id)), \
         patch("db.queries.consent.get_consent", new_callable=AsyncMock, return_value=None), \
         patch("handlers.consent.show_consent_optin", new_callable=AsyncMock) as mock_optin:
        from handlers.twin import cmd_me
        await cmd_me(message)

    mock_optin.assert_called_once_with(message)


@pytest.mark.asyncio
async def test_consent_gap_opt_in_false():
    """T3+ пользователь с opt_in=False видит экран согласия, не пустой дашборд."""
    message = _mock_message()
    account_id = uuid.uuid4()

    with patch("handlers.twin.get_intern", new_callable=AsyncMock, return_value=None), \
         patch("core.tier_detector.detect_ui_tier", new_callable=AsyncMock, return_value=3), \
         patch("helpers.dual_write.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value=str(account_id)), \
         patch("db.queries.consent.get_consent", new_callable=AsyncMock,
               return_value={"opt_in": False}), \
         patch("handlers.consent.show_consent_optin", new_callable=AsyncMock) as mock_optin:
        from handlers.twin import cmd_me
        await cmd_me(message)

    mock_optin.assert_called_once_with(message)


@pytest.mark.asyncio
async def test_consent_gap_opt_in_true_no_gate():
    """T3+ пользователь с opt_in=True не встречает consent-gate (gate не срабатывает).

    Без gateway-профиля и fallback-engagement функция уходит дальше в базовый
    dashboard (activity/learning/feed) — эти DB-запросы тоже мокаются, т.к. это
    не предмет теста, а обязательный проход мимо consent-gate до message.answer().
    """
    message = _mock_message()
    account_id = uuid.uuid4()

    with patch("handlers.twin.get_intern", new_callable=AsyncMock, return_value=None), \
         patch("core.tier_detector.detect_ui_tier", new_callable=AsyncMock, return_value=3), \
         patch("helpers.dual_write.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value=str(account_id)), \
         patch("db.queries.consent.get_consent", new_callable=AsyncMock,
               return_value={"opt_in": True}), \
         patch("handlers.consent.show_consent_optin", new_callable=AsyncMock) as mock_optin, \
         patch("clients.gateway_mcp.gateway_mcp.is_connected", return_value=False), \
         patch("handlers.twin._fallback_engagement", new_callable=AsyncMock, return_value=None), \
         patch("db.queries.activity.get_activity_stats", new_callable=AsyncMock, return_value={}), \
         patch("db.queries.answers.get_weekly_marathon_stats", new_callable=AsyncMock, return_value={}), \
         patch("db.queries.answers.get_weekly_feed_stats", new_callable=AsyncMock, return_value={}):
        from handlers.twin import cmd_me
        await cmd_me(message)

    mock_optin.assert_not_called()
