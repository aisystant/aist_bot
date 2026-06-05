"""Smoke-тесты: hermes_router (WP-392 Ф3.1).

«Гермес»/«hermes» → внешний Hermes-рантайм (Nous Research) через hermes_chat.
Роутер выделен из fallback и регистрируется ДО external_session/fallback.
"""

import contextlib

import pytest
from unittest.mock import AsyncMock, patch

from tests.smoke.conftest import make_intern
from tests.smoke.factories import text_message
from handlers.hermes import _is_hermes_message


@contextlib.asynccontextmanager
async def _noop_typing(*args, **kwargs):
    yield


@pytest.fixture(autouse=True)
def patch_hermes_deps():
    """Мокаем зависимости hermes_router: get_intern, marathon-guard, typing."""
    with patch("handlers.hermes.get_intern", new_callable=AsyncMock) as mock_get, \
         patch("handlers.external_session._sm_is_expecting_reply", new_callable=AsyncMock) as mock_sm, \
         patch("helpers.typing_indicator.keep_typing", _noop_typing):
        mock_get.return_value = make_intern(onboarding_completed=True, tier="T3", current_state=None)
        mock_sm.return_value = False
        yield {"get_intern": mock_get, "sm_expecting": mock_sm}


# ─── Фильтр ───

def test_filter_matches_hermes_prefix():
    for txt in ("Гермес, привет", "гермес статус", "Hermes hi", "  Гермес"):
        msg = text_message(txt, chat_id=1).message
        assert _is_hermes_message(msg) is True


def test_filter_rejects_non_hermes():
    for txt in ("привет", "/start", "что по WP-392", "Герметичность"):
        msg = text_message(txt, chat_id=1).message
        # «Герметичность» начинается с «гермет», не «гермес» → False
        assert _is_hermes_message(msg) is (txt.lower().startswith(("гермес", "hermes")))


# ─── Tier-gate ───

@pytest.mark.asyncio
async def test_hermes_tier1_blocked(bot, dp, patch_hermes_deps):
    """T1-пользователь получает сообщение о недоступности, hermes_chat НЕ вызывается."""
    patch_hermes_deps["get_intern"].return_value = make_intern(
        onboarding_completed=True, tier="T1", current_state=None
    )
    mock_hermes = AsyncMock(return_value="не должно вызваться")
    with patch("clients.gateway_mcp.gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        await dp.feed_update(bot, text_message("Гермес какой статус WP-392?", chat_id=12345))

    mock_hermes.assert_not_called()
    msgs = bot.get_sent("send_message")
    assert any("недоступна" in (m.get("text") or "") for m in msgs)


@pytest.mark.asyncio
async def test_hermes_tier3_calls_hermes(bot, dp, patch_hermes_deps):
    """T3-пользователь получает ответ от hermes_chat; prefix снимается."""
    patch_hermes_deps["get_intern"].return_value = make_intern(
        onboarding_completed=True, tier="T3", current_state=None
    )
    mock_hermes = AsyncMock(return_value="Статус WP-392: в работе")
    with patch("clients.gateway_mcp.gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        await dp.feed_update(bot, text_message("Гермес, какой статус WP-392?", chat_id=12345))

    mock_hermes.assert_called_once()
    call_args = mock_hermes.call_args
    assert "гермес" not in call_args.kwargs.get("message", "").lower()
    msgs = bot.get_sent("send_message")
    assert any("Статус WP-392" in (m.get("text") or "") for m in msgs)


@pytest.mark.asyncio
async def test_hermes_marathon_guard_skips(bot, dp, patch_hermes_deps):
    """Если marathon SM ждёт ответ — hermes_router пропускает (SkipHandler).

    После SkipHandler сообщение уходит в fallback — домокиваем его зависимости,
    чтобы downstream не лез в реальную БД.
    """
    patch_hermes_deps["sm_expecting"].return_value = True
    mock_hermes = AsyncMock(return_value="не должно вызваться")
    with patch("clients.gateway_mcp.gateway_mcp") as mock_gmc, \
         patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True)), \
         patch("handlers.get_dispatcher", return_value=None):
        mock_gmc.hermes_chat = mock_hermes
        await dp.feed_update(bot, text_message("Гермес, привет", chat_id=12345))

    mock_hermes.assert_not_called()
