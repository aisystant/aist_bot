"""Unit tests WP-5 F78: one digest build per chat.

Pilot, prod 12.11: after "Topics selected!" the bot showed "Preparing material..." twice. Confirming
topics already moves the reader into the digest and starts the build; the confirmation message still
offered a "Get digest" button, and tapping it started a second model build in parallel.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from config import FeedStatus  # noqa: E402
from i18n import t  # noqa: E402
from states.feed import digest as digest_module  # noqa: E402
from states.feed.digest import FeedDigestState  # noqa: E402
from states.feed.topics import FeedTopicsState  # noqa: E402

CHAT_ID = 5151
WEEK = {"id": 1, "status": "active", "accepted_topics": ["Роли"], "current_day": 1}
CONTENT = {"main_content": "текст", "topics_list": ["Роли"]}


def make_intern() -> dict:
    return {"chat_id": CHAT_ID, "language": "ru", "feed_status": FeedStatus.ACTIVE,
            "marathon_status": "completed", "feed_duration": 10}


def make_state() -> FeedDigestState:
    state = FeedDigestState.__new__(FeedDigestState)
    state.send = AsyncMock()
    return state


def sent_texts(state) -> list:
    return [c.args[1] for c in state.send.await_args_list]


@pytest.fixture
def digest_db():
    """No digest for today yet; building takes a moment."""
    async def slow_generate(**kwargs):
        await asyncio.sleep(0.2)
        return CONTENT

    with patch.object(digest_module, "get_current_feed_week", new=AsyncMock(return_value=dict(WEEK))), \
         patch.object(digest_module, "get_feed_session", new=AsyncMock(return_value=None)), \
         patch.object(digest_module, "create_feed_session", new=AsyncMock(return_value={"id": 9, "content": CONTENT})), \
         patch.object(digest_module, "generate_multi_topic_digest", new=AsyncMock(side_effect=slow_generate)) as generate, \
         patch.object(FeedDigestState, "_show_digest", new=AsyncMock()) as show_digest:
        yield generate, show_digest


@pytest.mark.asyncio
async def test_second_entry_during_build_does_not_start_another(digest_db):
    generate, show_digest = digest_db
    first, second = make_state(), make_state()

    await asyncio.gather(first.enter(make_intern()), second.enter(make_intern()))

    assert generate.await_count == 1  # the model runs once
    assert show_digest.await_count == 1  # the digest is shown once
    assert sent_texts(second) == [t("feed.digest_in_progress", "ru")]
    assert CHAT_ID not in digest_module._digest_in_progress


@pytest.mark.asyncio
async def test_chat_is_released_when_the_build_fails(digest_db):
    generate, _ = digest_db
    generate.side_effect = RuntimeError("model down")
    state = make_state()

    await state.enter(make_intern())

    assert t("errors.try_again", "ru") in sent_texts(state)
    assert CHAT_ID not in digest_module._digest_in_progress  # next /feed can build again


@pytest.mark.asyncio
async def test_topics_confirmation_has_no_get_digest_button():
    state = FeedTopicsState.__new__(FeedTopicsState)
    state.bot = MagicMock(send_message=AsyncMock())

    with patch("states.feed.topics.get_current_feed_week", new=AsyncMock(return_value=dict(WEEK))), \
         patch("states.feed.topics.delete_feed_sessions", new=AsyncMock()), \
         patch("states.feed.topics.update_feed_week", new=AsyncMock()):
        accepted = await state._accept_topics(CHAT_ID, ["Роли"], "ru")

    assert accepted is True
    call = state.bot.send_message.await_args
    assert call.args[0] == CHAT_ID
    assert "reply_markup" not in call.kwargs
