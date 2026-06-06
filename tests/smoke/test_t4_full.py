"""
Smoke-тесты: WP-392 Ф3.1b — T4-full mode (всё в Hermes, без префикса).

Критерии приёмки:
- T4: «привет» → ответ Hermes, консультант не вызывается
- T4: «напомни про завтрак» → ответ Hermes
- T4: /mydata → платформа (не Hermes)
- T3: «привет» → не Hermes (консультант или SM)
- T3: «Гермес, привет» → Hermes
- T0-T2: «?вопрос» → консультант
- T0-T2: «Гермес, привет» → «недоступно на твоём тире»

Тесты используют прямой вызов on_unknown_message (unit-style),
т.к. dp.feed_update требует полного pipeline aiogram с моками
на всех уровнях (hermes_router, commands, DB pool).
"""

import contextlib
import importlib

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

# clients/__init__.py re-exports `gateway_mcp` as instance → patch("clients.gateway_mcp.gateway_mcp")
# resolves to the instance, not the submodule. Use patch.object on the real submodule instead.
_gmc_mod = importlib.import_module("clients.gateway_mcp")

from tests.smoke.conftest import make_intern
from handlers.fallback import on_unknown_message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Chat, User as TgUser
from datetime import datetime


@contextlib.asynccontextmanager
async def _noop_typing(*args, **kwargs):
    yield


def _make_msg(text: str, chat_id: int = 12345) -> Message:
    """Создаёт Message с monkey-patched answer (обход frozen pydantic)."""
    msg = Message.model_construct(
        message_id=1,
        date=datetime.now(),
        chat=Chat.model_construct(id=chat_id, type="private"),
        from_user=TgUser.model_construct(id=chat_id, is_bot=False, first_name="Test"),
        text=text,
    )
    object.__setattr__(msg, "answer", AsyncMock())
    return msg


@pytest.fixture
def state():
    return FSMContext(storage=MemoryStorage(), key="test")


@pytest.fixture(autouse=True)
def patch_deps():
    """Мокаем typing indicator и gateway."""
    with patch("helpers.typing_indicator.keep_typing", _noop_typing):
        yield


# ─── T4-full: обычный текст → Hermes ───

@pytest.mark.asyncio
async def test_t4_plain_text_goes_to_hermes(state):
    """T4: «привет» → hermes_chat, консультант не вызывается."""
    msg = _make_msg("привет")
    mock_hermes = AsyncMock(return_value="Привет! Чем могу помочь?")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T4", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        await on_unknown_message(msg, state)

    mock_hermes.assert_called_once()
    call_kwargs = mock_hermes.call_args.kwargs
    assert call_kwargs.get("message") == "привет"
    assert call_kwargs.get("telegram_user_id") == 12345
    msg.answer.assert_awaited_once()
    assert "Привет!" in msg.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_t4_reminder_goes_to_hermes(state):
    """T4: «напомни про завтрак» → hermes_chat."""
    msg = _make_msg("напомни про завтрак")
    mock_hermes = AsyncMock(return_value="Используй /remind для напоминаний.")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T4", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        await on_unknown_message(msg, state)

    mock_hermes.assert_called_once()
    assert mock_hermes.call_args.kwargs.get("message") == "напомни про завтрак"


@pytest.mark.asyncio
async def test_t4_command_skips_hermes(state):
    """T4: /mydata → платформа (не Hermes)."""
    msg = _make_msg("/mydata")
    mock_hermes = AsyncMock(return_value="не должно вызваться")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T4", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        await on_unknown_message(msg, state)

    mock_hermes.assert_not_called()
    mock_dp.route_message.assert_awaited_once()


# ─── T3: обычный текст → НЕ Hermes ───

@pytest.mark.asyncio
async def test_t3_plain_text_not_hermes(state):
    """T3: «привет» → консультант/SM, hermes_chat НЕ вызывается."""
    msg = _make_msg("привет")
    mock_hermes = AsyncMock(return_value="не должно вызваться")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T3", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        await on_unknown_message(msg, state)

    mock_hermes.assert_not_called()
    mock_dp.route_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_t3_hermes_prefix_calls_hermes(state):
    """T3: «Гермес, привет» → hermes_chat через fallback fail-safe."""
    msg = _make_msg("Гермес, привет")
    mock_hermes = AsyncMock(return_value="Привет от Hermes!")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T3", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        await on_unknown_message(msg, state)

    mock_hermes.assert_called_once()
    assert "привет" in mock_hermes.call_args.kwargs.get("message", "").lower()
    msg.answer.assert_awaited_once()
    assert "Привет от Hermes!" in msg.answer.await_args.args[0]


# ─── T0-T2: префикс → отказ ───

@pytest.mark.asyncio
async def test_t1_hermes_prefix_blocked(state):
    """T1: «Гермес, привет» → «недоступно на твоём тире»."""
    msg = _make_msg("Гермес, привет")
    mock_hermes = AsyncMock(return_value="не должно вызваться")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T1", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        await on_unknown_message(msg, state)

    mock_hermes.assert_not_called()
    msg.answer.assert_awaited_once()
    assert "недоступна" in msg.answer.await_args.args[0]


# ─── T4 session_id передаётся ───

@pytest.mark.asyncio
async def test_t4_session_id_passed(state):
    """T4-full: session_id передаётся в hermes_chat (для памяти диалога)."""
    msg = _make_msg("привет")
    mock_hermes = AsyncMock(return_value="Ответ с памятью")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T4", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        mock_gmc.hermes_chat = mock_hermes
        await on_unknown_message(msg, state)

    call_kwargs = mock_hermes.call_args.kwargs
    assert "session_id" in call_kwargs
    # Первый вызов — session_id=None (новая сессия)
    assert call_kwargs.get("session_id") is None


@pytest.mark.asyncio
async def test_t4_session_id_reused(state):
    """T4-full: при повторном вызове передаётся сохранённый session_id."""
    from handlers.fallback import _HERMES_SESSION_MAP

    _HERMES_SESSION_MAP[12345] = "sess-abc-123"
    try:
        msg = _make_msg("а что дальше?")
        mock_hermes = AsyncMock(return_value="Дальше — действуй")
        mock_dp = MagicMock()
        mock_dp.is_sm_active = True
        mock_dp.route_message = AsyncMock(return_value=True)

        with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
                   return_value=make_intern(onboarding_completed=True, tier="T4", current_state=None)), \
             patch("handlers.get_dispatcher", return_value=mock_dp), \
             patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
            mock_gmc.hermes_chat = mock_hermes
            await on_unknown_message(msg, state)

        assert mock_hermes.call_args.kwargs.get("session_id") == "sess-abc-123"
    finally:
        _HERMES_SESSION_MAP.pop(12345, None)
