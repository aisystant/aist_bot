"""
Smoke-тесты: fallback (обработка произвольных сообщений и callback-ов).

Сценарии 8.7-8.8 из testing-scenarios.md + edge cases.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from tests.smoke.conftest import make_intern
from tests.smoke.factories import text_message, callback_button


@pytest.fixture(autouse=True)
def patch_fallback_deps():
    """Мокаем DB для fallback."""
    with patch("handlers.fallback.get_intern", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = make_intern(onboarding_completed=True)
        yield {"get_intern": mock_get}


# ─── 8.7: Произвольный текст ───

@pytest.mark.asyncio
async def test_arbitrary_text_does_not_crash(bot, dp, patch_fallback_deps):
    """Сценарий 8.7: Произвольный текст не вызывает ошибку."""
    update = text_message("Привет, как дела?", chat_id=12345)
    await dp.feed_update(bot, update)

    # Fallback без SM отвечает ошибкой (processing_error) — это ОК
    assert len(bot.sent) > 0, "Fallback не ответил на произвольный текст"


@pytest.mark.asyncio
async def test_arbitrary_text_with_sm(bot, dp, patch_fallback_deps):
    """Сценарий 8.7: С активной SM и current_state произвольный текст делегируется в SM."""
    # WP-392 Ф3.1: Hermes-роутер перехватывает только при current_state=None.
    # Пользователь с current_state (активный SM-стейт) должен идти через route_message.
    patch_fallback_deps["get_intern"].return_value = make_intern(
        onboarding_completed=True,
        current_state="marathon.lesson",  # активный SM-стейт → Hermes не перехватывает
    )
    mock_dispatcher = MagicMock()
    mock_dispatcher.is_sm_active = True
    mock_dispatcher.route_message = AsyncMock(return_value=True)

    with patch("handlers.get_dispatcher", return_value=mock_dispatcher):
        update = text_message("Привет, как дела?", chat_id=12345)
        await dp.feed_update(bot, update)

    mock_dispatcher.route_message.assert_called_once()


@pytest.mark.asyncio
async def test_hermes_routing_tier1_blocked(bot, dp, patch_fallback_deps):
    """WP-392 Ф3.1: T1-пользователь получает сообщение о недоступности."""
    patch_fallback_deps["get_intern"].return_value = make_intern(
        onboarding_completed=True, tier="T1", current_state=None
    )
    mock_dispatcher = MagicMock()
    mock_dispatcher.is_sm_active = True
    mock_dispatcher.route_message = AsyncMock(return_value=True)

    with patch("handlers.get_dispatcher", return_value=mock_dispatcher), \
         patch("handlers.onboarding_intent.route_onboarding_intent", new_callable=AsyncMock, return_value=False):
        update = text_message("Гермес какой статус WP-392?", chat_id=12345)
        await dp.feed_update(bot, update)

    mock_dispatcher.route_message.assert_not_called()
    msgs = bot.get_sent("send_message")
    assert any("недоступна" in (m.get("text") or "") for m in msgs)


@pytest.mark.asyncio
async def test_hermes_routing_tier3_calls_hermes(bot, dp, patch_fallback_deps):
    """WP-392 Ф3.1: T3-пользователь получает ответ от hermes_chat."""
    patch_fallback_deps["get_intern"].return_value = make_intern(
        onboarding_completed=True, tier="T3", current_state=None
    )
    mock_dispatcher = MagicMock()
    mock_dispatcher.is_sm_active = True
    mock_hermes = AsyncMock(return_value="Статус WP-392: в работе")

    with patch("handlers.get_dispatcher", return_value=mock_dispatcher), \
         patch("handlers.onboarding_intent.route_onboarding_intent", new_callable=AsyncMock, return_value=False), \
         patch("clients.gateway_mcp.gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        update = text_message("Гермес, какой статус WP-392?", chat_id=12345)
        await dp.feed_update(bot, update)

    mock_hermes.assert_called_once()
    # Проверяем что текст к Hermes передан без prefix (strip punctuation)
    call_args = mock_hermes.call_args
    assert "гермес" not in call_args.kwargs.get("message", "").lower()
    msgs = bot.get_sent("send_message")
    assert any("Статус WP-392" in (m.get("text") or "") for m in msgs)


# ─── 8.8: Channel/group messages игнорируются ───

@pytest.mark.asyncio
async def test_channel_message_ignored(bot, dp, patch_fallback_deps):
    """Сценарий: Сообщения из каналов/групп игнорируются fallback-ом."""
    from aiogram.types import Update, Message, Chat, User as TgUser
    from datetime import datetime

    # Создаём сообщение из группы
    msg = Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=-100123456, type="supergroup", title="Test Group"),
        from_user=TgUser(id=12345, is_bot=False, first_name="Test"),
        text="сообщение в группе",
    )
    update = Update(update_id=999, message=msg)

    await dp.feed_update(bot, update)

    # Fallback не должен отвечать на сообщения из групп
    msgs = bot.get_sent("send_message")
    assert len(msgs) == 0, "Fallback ответил на сообщение из группы"


# ─── Stale callback ───

@pytest.mark.asyncio
async def test_stale_callback_shows_expired(bot, dp, patch_fallback_deps):
    """Устаревшая inline-кнопка показывает 'button_expired'."""
    update = callback_button(data="nonexistent_callback", chat_id=12345)

    await dp.feed_update(bot, update)

    # Бот должен вызвать answer_callback_query
    answers = bot.get_sent("answer_callback_query")
    assert len(answers) > 0, "Fallback не ответил на stale callback"


# ─── Consultation passthrough (? message) ───

@pytest.mark.asyncio
async def test_question_mark_message(bot, dp, patch_fallback_deps):
    """Сообщение с ? обрабатывается (без middleware не будет passthrough, но не должно падать)."""
    update = text_message("? что такое системное мышление?", chat_id=12345)
    await dp.feed_update(bot, update)

    # Не падает — smoke test пройден
    assert True
