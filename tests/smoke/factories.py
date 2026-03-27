"""
Update factories — строители Telegram Update объектов для smoke-тестов.

Создают валидные aiogram-совместимые Update объекты для dp.feed_update().
"""

from datetime import datetime

from aiogram.types import (
    Update, Message, CallbackQuery,
    Chat, User as TgUser,
    InlineKeyboardMarkup, InlineKeyboardButton,
)


# ─── Базовые сущности ───

_update_id_counter = 0


def _next_update_id() -> int:
    global _update_id_counter
    _update_id_counter += 1
    return _update_id_counter


def make_user(
    user_id: int = 12345,
    first_name: str = "Тестовый",
    username: str = "testuser",
    language_code: str = "ru",
) -> TgUser:
    return TgUser(
        id=user_id,
        is_bot=False,
        first_name=first_name,
        username=username,
        language_code=language_code,
    )


def make_chat(chat_id: int = 12345) -> Chat:
    return Chat(id=chat_id, type="private")


# ─── Message Updates ───

def make_message(
    text: str = "hello",
    chat_id: int = 12345,
    user_id: int = 12345,
    message_id: int = 1,
    **user_kwargs,
) -> Message:
    """Создаёт Message объект."""
    return Message(
        message_id=message_id,
        date=datetime.now(),
        chat=make_chat(chat_id),
        from_user=make_user(user_id=user_id, **user_kwargs),
        text=text,
    )


def make_message_update(
    text: str = "hello",
    chat_id: int = 12345,
    user_id: int = 12345,
    **kwargs,
) -> Update:
    """Создаёт Update с Message (текстовое сообщение)."""
    return Update(
        update_id=_next_update_id(),
        message=make_message(text=text, chat_id=chat_id, user_id=user_id, **kwargs),
    )


def make_command_update(
    command: str = "/start",
    chat_id: int = 12345,
    user_id: int = 12345,
    **kwargs,
) -> Update:
    """Создаёт Update с командой (/start, /learn, /mode, etc.)."""
    return make_message_update(
        text=command,
        chat_id=chat_id,
        user_id=user_id,
        **kwargs,
    )


# ─── Callback Query Updates ───

def make_callback_update(
    data: str = "some_callback",
    chat_id: int = 12345,
    user_id: int = 12345,
    message_text: str = "Предыдущее сообщение",
    message_id: int = 100,
) -> Update:
    """Создаёт Update с CallbackQuery (нажатие inline-кнопки)."""
    message = Message(
        message_id=message_id,
        date=datetime.now(),
        chat=make_chat(chat_id),
        from_user=make_user(user_id=0),  # bot sent the message
        text=message_text,
    )

    callback = CallbackQuery(
        id=f"callback_{_next_update_id()}",
        chat_instance=str(chat_id),
        from_user=make_user(user_id=user_id),
        message=message,
        data=data,
    )

    return Update(
        update_id=_next_update_id(),
        callback_query=callback,
    )


# ─── Convenience: типичные сценарии ───

def start_command(chat_id: int = 12345) -> Update:
    """/start для нового пользователя."""
    return make_command_update("/start", chat_id=chat_id, user_id=chat_id)


def learn_command(chat_id: int = 12345) -> Update:
    """/learn — запрос урока."""
    return make_command_update("/learn", chat_id=chat_id, user_id=chat_id)


def mode_command(chat_id: int = 12345) -> Update:
    """/mode — главное меню."""
    return make_command_update("/mode", chat_id=chat_id, user_id=chat_id)


def help_command(chat_id: int = 12345) -> Update:
    """/help — справка."""
    return make_command_update("/help", chat_id=chat_id, user_id=chat_id)


def progress_command(chat_id: int = 12345) -> Update:
    """/progress — статистика."""
    return make_command_update("/progress", chat_id=chat_id, user_id=chat_id)


def settings_command(chat_id: int = 12345) -> Update:
    """/settings — настройки."""
    return make_command_update("/settings", chat_id=chat_id, user_id=chat_id)


def text_message(text: str, chat_id: int = 12345) -> Update:
    """Произвольное текстовое сообщение."""
    return make_message_update(text=text, chat_id=chat_id, user_id=chat_id)


def callback_button(data: str, chat_id: int = 12345) -> Update:
    """Нажатие inline-кнопки."""
    return make_callback_update(data=data, chat_id=chat_id, user_id=chat_id)


def language_callback(lang: str = "ru", chat_id: int = 12345) -> Update:
    """Выбор языка при онбординге."""
    return make_callback_update(data=f"lang_{lang}", chat_id=chat_id, user_id=chat_id)


def duration_callback(duration: str = "15", chat_id: int = 12345) -> Update:
    """Выбор длительности при онбординге."""
    return make_callback_update(data=f"duration_{duration}", chat_id=chat_id, user_id=chat_id)
