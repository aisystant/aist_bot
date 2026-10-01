"""The update dedup runs once per update, not once per matched handler.

An inner middleware runs for every handler that matches. A handler that raises SkipHandler passes the
update on, and the inner chain runs again for the next handler. Registered as an inner middleware, the
dedup took that second pass for a webhook retry and swallowed every private free-text message on the
pilot bot (2026-10-01: on_mentor_dm_free_text raises SkipHandler for everyone who is not a mentor).

These tests go through a real aiogram Dispatcher and the real install_update_dedup().
"""

import os
import sys
from datetime import datetime

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from aiogram import Bot, Dispatcher, F, Router  # noqa: E402
from aiogram.dispatcher.event.bases import SkipHandler  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User  # noqa: E402

from core.middleware import install_update_dedup  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 33, 0)
USER = User(id=1, is_bot=False, first_name="t")
CHAT = Chat(id=1, type="private")


def message_update(update_id: int) -> Update:
    message = Message(message_id=update_id, date=NOW, chat=CHAT, from_user=USER, text="? кто такой наставник?")
    return Update(update_id=update_id, message=message)


def button_update(update_id: int) -> Update:
    message = Message(message_id=1, date=NOW, chat=CHAT, from_user=USER, text="digest")
    callback = CallbackQuery(id=str(update_id), from_user=USER, chat_instance="c", message=message, data="feed_more")
    return Update(update_id=update_id, callback_query=callback)


def build_dispatcher() -> tuple[Dispatcher, list[str]]:
    """Two routers in the order of the real bot: the first matches and passes the update on, the second answers."""
    dp = Dispatcher()
    install_update_dedup(dp)
    calls: list[str] = []
    passes_on, answers = Router(), Router()

    @passes_on.message(F.text)
    async def skip_message(message: Message) -> None:
        calls.append("skip")
        raise SkipHandler

    @passes_on.callback_query()
    async def skip_button(callback: CallbackQuery) -> None:
        calls.append("skip")
        raise SkipHandler

    @answers.message()
    async def answer_message(message: Message) -> None:
        calls.append("answer")

    @answers.callback_query()
    async def answer_button(callback: CallbackQuery) -> None:
        calls.append("answer")

    dp.include_router(passes_on)
    dp.include_router(answers)
    return dp, calls


async def feed(dp: Dispatcher, *updates: Update) -> None:
    bot = Bot(token="000000000:AAFakeTokenForTests")
    try:
        for update in updates:
            await dp.feed_update(bot, update)
    finally:
        await bot.session.close()


UPDATE_KINDS = pytest.mark.parametrize("make_update", [message_update, button_update], ids=["message", "button"])


@UPDATE_KINDS
@pytest.mark.asyncio
async def test_skip_reaches_next_handler(make_update):
    dp, calls = build_dispatcher()

    await feed(dp, make_update(1))

    assert calls == ["skip", "answer"]


@UPDATE_KINDS
@pytest.mark.asyncio
async def test_real_retry_is_dropped(make_update):
    dp, calls = build_dispatcher()

    await feed(dp, make_update(1), make_update(1))  # Telegram delivers the same update_id twice

    assert calls == ["skip", "answer"]


@UPDATE_KINDS
@pytest.mark.asyncio
async def test_next_update_is_handled(make_update):
    dp, calls = build_dispatcher()

    await feed(dp, make_update(1), make_update(2))

    assert calls == ["skip", "answer", "skip", "answer"]
