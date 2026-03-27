"""
SM test helpers — создание стейтов с замоканными зависимостями.

Позволяет тестировать SM-стейты напрямую: state.enter(), state.handle(), state.handle_callback().
"""

from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, date

from aiogram.types import Message, Chat, User as TgUser, CallbackQuery

from tests.smoke.conftest import ThinMockBot, make_intern


def create_state(state_class, bot=None):
    """Создаёт SM-стейт с замоканными зависимостями.

    Args:
        state_class: Класс стейта (MarathonLessonState, ModeSelectState, etc.)
        bot: ThinMockBot (создаётся если не передан)

    Returns:
        (state, bot) — инстанс стейта и бот для assert-ов
    """
    if bot is None:
        bot = ThinMockBot()

    # i18n mock — t() возвращает ключ с параметрами для простоты
    i18n_mock = MagicMock()
    i18n_mock.t = lambda key, lang='ru', **kwargs: f"[{key}]"

    state = state_class(
        bot=bot,
        db=None,
        llm=None,
        i18n=i18n_mock,
    )

    return state, bot


def make_message_obj(text="hello", chat_id=12345, user_id=12345):
    """Создаёт Message объект для передачи в state.handle()."""
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Test", username="test"),
        text=text,
    )


def make_callback_obj(data="some_data", chat_id=12345, user_id=12345, message_text="prev", bot=None):
    """Создаёт CallbackQuery объект для передачи в state.handle_callback().

    Если bot передан — callback mount-ится к bot instance (нужно для callback.answer()).
    """
    msg = Message(
        message_id=100,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=0, is_bot=True, first_name="Bot"),
        text=message_text,
    )
    cb = CallbackQuery(
        id=f"cb_{data}",
        chat_instance=str(chat_id),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Test", username="test"),
        message=msg,
        data=data,
    )
    if bot is not None:
        # Bypass pydantic frozen model to mock answer() and message.answer()
        object.__setattr__(cb, 'answer', AsyncMock())
        # Also mock message methods that call bot
        object.__setattr__(cb.message, 'answer', AsyncMock(return_value=_make_msg_response(chat_id)))
        object.__setattr__(cb.message, 'edit_text', AsyncMock())
        object.__setattr__(cb.message, 'edit_reply_markup', AsyncMock())
        object.__setattr__(cb.message, 'delete', AsyncMock())
    return cb


def _make_msg_response(chat_id):
    """Minimal Message response for mocked message.answer()."""
    return Message(
        message_id=999,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=0, is_bot=True, first_name="Bot"),
        text="response",
    )
