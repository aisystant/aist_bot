"""
Smoke-тесты: T4 fallback-роутинг после отключения Hermes (WP-392 retirement, 05.09).

До 05.09 T4-full маршрутизировал ВЕСЬ обычный текст в Hermes, минуя SM/консультацию
(WP-392 Ф3.1b). Пилот решил отключить внешний Hermes-рантайм в чате бота — T4 теперь
ведёт себя как любой другой тир: обычный текст идёт в SM (dispatcher.route_message),
gateway_mcp.hermes_chat из fallback.py больше не вызывается никогда.

Критерии приёмки:
- T4: «привет» → SM/консультант (не Hermes)
- T4: /mydata → платформа (как раньше)
- T4: «Гермес, привет» без активного SM-ожидания → перехватывается hermes_router
  (handlers/hermes.py), fallback.py его не видит и не вызывает gateway_mcp
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


@pytest.mark.asyncio
async def test_t4_plain_text_goes_to_sm_not_hermes(state):
    """T4: «привет» → SM (как T1-T3), hermes_chat НЕ вызывается."""
    msg = _make_msg("привет")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T4", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        await on_unknown_message(msg, state)

    mock_gmc.hermes_chat.assert_not_called()
    mock_dp.route_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_t4_command_skips_sm_text_routing(state):
    """T4: /mydata → платформа (не задета отключением Hermes)."""
    msg = _make_msg("/mydata")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T4", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        await on_unknown_message(msg, state)

    mock_gmc.hermes_chat.assert_not_called()
    mock_dp.route_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_t4_hermes_prefix_never_reaches_fallback_gateway_call(state):
    """T4: «Гермес, привет» — fallback.py больше не содержит fail-safe вызова

    gateway_mcp.hermes_chat (удалён вместе с T4-full блоком). В проде такое
    сообщение перехватывается hermes_router (handlers/hermes.py) раньше —
    здесь проверяем только то, что fallback сам по себе безопасен: если
    сообщение всё же дошло сюда (edge case регистрации роутеров), оно уходит
    в обычный SM-путь, а не в Hermes.
    """
    msg = _make_msg("Гермес, привет")
    mock_dp = MagicMock()
    mock_dp.is_sm_active = True
    mock_dp.route_message = AsyncMock(return_value=True)

    with patch("handlers.fallback.get_intern", new_callable=AsyncMock,
               return_value=make_intern(onboarding_completed=True, tier="T4", current_state=None)), \
         patch("handlers.get_dispatcher", return_value=mock_dp), \
         patch.object(_gmc_mod, "gateway_mcp") as mock_gmc:
        await on_unknown_message(msg, state)

    mock_gmc.hermes_chat.assert_not_called()
    mock_dp.route_message.assert_awaited_once()
