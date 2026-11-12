"""Unit tests WP-5 F76: pause / resume the daily feed digest.

Before F76 a reader could not stop the daily digest at all: feed_status='paused' existed and the
scheduler already skipped it, but no button ever set it. These tests pin the observable effects:
which status and mode get written, that repeated taps write nothing, which buttons appear where,
and that the scheduler sends nothing to a paused reader.
"""

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
from core.scheduler import feed_notification_keyboard, pre_generate_feed_digest  # noqa: E402
from handlers.callbacks import cb_feed_actions  # noqa: E402
from i18n import t  # noqa: E402
from states.feed import pause  # noqa: E402
from states.feed.digest import FeedDigestState  # noqa: E402
from states.feed.topics import FeedTopicsState  # noqa: E402

CHAT_ID = 7676


def make_intern(feed_status: str, marathon_status: str = "completed", **extra) -> dict:
    return {
        "chat_id": CHAT_ID, "language": "ru", "feed_status": feed_status,
        "marathon_status": marathon_status, "mode": "both", "current_state": "common.mode_select",
        "current_context": {}, **extra,
    }


def make_callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.message.chat.id = CHAT_ID
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def callbacks_of(markup) -> list:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.fixture
def db():
    """Patch the two writes the toggle makes; yields the mocks."""
    with patch.object(pause, "update_intern", new=AsyncMock()) as update_intern, \
         patch.object(pause, "log_event", new=AsyncMock()) as log_event:
        yield update_intern, log_event


# =============================================================================
# set_feed_status: what gets written
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("marathon_status, expected_mode", [("completed", "marathon"), ("not_started", "marathon")])
async def test_pause_writes_paused_status_and_mode_without_feed(db, marathon_status, expected_mode):
    update_intern, log_event = db

    changed = await pause.set_feed_status(make_intern(FeedStatus.ACTIVE, marathon_status), FeedStatus.PAUSED)

    assert changed is True
    update_intern.assert_awaited_once_with(CHAT_ID, feed_status=FeedStatus.PAUSED, mode=expected_mode)
    event_type, payload = log_event.await_args.args[1], log_event.await_args.args[2]
    assert event_type == "mode_changed"
    assert payload == {"from_mode": "both", "to_mode": expected_mode, "trigger": "feed_pause"}


@pytest.mark.asyncio
@pytest.mark.parametrize("marathon_status, expected_mode", [("completed", "both"), ("not_started", "feed")])
async def test_resume_writes_active_status_and_mode_with_feed(db, marathon_status, expected_mode):
    update_intern, log_event = db

    changed = await pause.set_feed_status(make_intern(FeedStatus.PAUSED, marathon_status), FeedStatus.ACTIVE)

    assert changed is True
    update_intern.assert_awaited_once_with(CHAT_ID, feed_status=FeedStatus.ACTIVE, mode=expected_mode)
    assert log_event.await_args.args[2]["trigger"] == "feed_resume"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [FeedStatus.PAUSED, FeedStatus.ACTIVE])
async def test_repeated_transition_writes_nothing(db, status):
    update_intern, log_event = db

    changed = await pause.set_feed_status(make_intern(status), status)

    assert changed is False
    update_intern.assert_not_awaited()
    log_event.assert_not_awaited()


# =============================================================================
# handle_toggle: what the reader sees
# =============================================================================

@pytest.mark.asyncio
async def test_pause_tap_confirms_and_offers_resume(db):
    callback = make_callback(pause.PAUSE_CALLBACK)

    await pause.handle_toggle(callback, make_intern(FeedStatus.ACTIVE))

    text = callback.message.answer.await_args.args[0]
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert text == t("feed.paused_confirm", "ru")
    assert callbacks_of(markup) == [pause.RESUME_CALLBACK]
    callback.message.edit_reply_markup.assert_not_awaited()  # tapped menu keeps its other buttons


@pytest.mark.asyncio
async def test_resume_tap_confirms_and_offers_pause_again(db):
    callback = make_callback(pause.RESUME_CALLBACK)

    await pause.handle_toggle(callback, make_intern(FeedStatus.PAUSED))

    assert callback.message.answer.await_args.args[0] == t("feed.resumed_confirm", "ru")
    assert callbacks_of(callback.message.answer.await_args.kwargs["reply_markup"]) == [pause.PAUSE_CALLBACK]


@pytest.mark.asyncio
async def test_repeated_pause_tap_shows_toast_only(db):
    update_intern, _ = db
    callback = make_callback(pause.PAUSE_CALLBACK)

    await pause.handle_toggle(callback, make_intern(FeedStatus.PAUSED))

    callback.answer.assert_awaited_once_with(t("feed.pause_already_paused", "ru"))
    callback.message.answer.assert_not_awaited()
    update_intern.assert_not_awaited()


# =============================================================================
# Routing: the toggle works from any state and never moves the state machine
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("current_state", ["common.consultation", "feed.digest", "common.mode_select"])
async def test_cb_feed_actions_routes_pause_from_any_state(db, current_state):
    update_intern, _ = db
    callback = make_callback(pause.PAUSE_CALLBACK)
    dispatcher = MagicMock(is_sm_active=True, go_to=AsyncMock(), route_callback=AsyncMock())
    intern = make_intern(FeedStatus.ACTIVE, current_state=current_state)

    with patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("handlers.callbacks.get_intern", new=AsyncMock(return_value=intern)):
        await cb_feed_actions(callback, AsyncMock())

    update_intern.assert_awaited_once()
    assert update_intern.await_args.kwargs["feed_status"] == FeedStatus.PAUSED
    dispatcher.go_to.assert_not_awaited()
    dispatcher.route_callback.assert_not_awaited()


# =============================================================================
# Buttons on screens
# =============================================================================

def test_notification_has_open_and_disable_buttons():
    assert callbacks_of(feed_notification_keyboard("ru")) == ["feed_get_digest", pause.PAUSE_CALLBACK]


@pytest.mark.parametrize("status, expected", [
    (FeedStatus.ACTIVE, [pause.PAUSE_CALLBACK]),
    (FeedStatus.PAUSED, [pause.RESUME_CALLBACK]),
    (FeedStatus.NOT_STARTED, []),
])
def test_toggle_row_follows_status(status, expected):
    rows = pause.toggle_row(status, "ru")
    assert [button.callback_data for row in rows for button in row] == expected


def make_state() -> FeedDigestState:
    state = FeedDigestState.__new__(FeedDigestState)
    state.send = AsyncMock()
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("status, toggle", [(FeedStatus.ACTIVE, pause.PAUSE_CALLBACK), (FeedStatus.PAUSED, pause.RESUME_CALLBACK)])
async def test_feed_menu_shows_toggle_for_status(status, toggle):
    state = make_state()

    await state._show_menu(make_intern(status), {"accepted_topics": ["Системное мышление"]})

    assert callbacks_of(state.send.await_args.kwargs["reply_markup"])[-1] == toggle


@pytest.mark.asyncio
async def test_topics_menu_shows_resume_when_paused():
    state = make_state()

    await state._show_topics_menu_standalone(make_intern(FeedStatus.PAUSED), {"accepted_topics": ["Роли"]})

    assert callbacks_of(state.send.await_args.kwargs["reply_markup"])[-1] == pause.RESUME_CALLBACK


@pytest.mark.asyncio
async def test_entering_feed_while_paused_shows_notice_and_writes_nothing():
    state = make_state()
    week = {"id": 1, "status": "active", "accepted_topics": ["Роли"], "current_day": 1}
    session = {"id": 5, "status": "active"}

    with patch("states.feed.digest.get_current_feed_week", new=AsyncMock(return_value=week)), \
         patch("states.feed.digest.get_feed_session", new=AsyncMock(return_value=session)), \
         patch("states.feed.digest.update_intern", new=AsyncMock()) as update_intern, \
         patch.object(FeedDigestState, "_show_digest", new=AsyncMock()) as show_digest:
        await state.enter(make_intern(FeedStatus.PAUSED))

    notice = state.send.await_args_list[0]
    assert notice.args[1] == t("feed.paused_notice", "ru")
    assert callbacks_of(notice.kwargs["reply_markup"]) == [pause.RESUME_CALLBACK]
    show_digest.assert_awaited_once()  # reading on demand still works while paused
    update_intern.assert_not_awaited()


# =============================================================================
# Scheduler: a paused reader gets no notification
# =============================================================================

@pytest.mark.asyncio
async def test_scheduler_sends_nothing_to_paused_reader():
    bot = MagicMock(send_message=AsyncMock())

    with patch("core.scheduler.get_intern", new=AsyncMock(return_value=make_intern(FeedStatus.PAUSED))), \
         patch("core.scheduler.get_current_feed_week", new=AsyncMock()) as get_week:
        await pre_generate_feed_digest(CHAT_ID, bot)

    get_week.assert_not_awaited()
    bot.send_message.assert_not_awaited()


# =============================================================================
# Entry via /feed goes through feed.topics first: it must not resume a paused feed
# =============================================================================

def make_topics_state() -> FeedTopicsState:
    state = FeedTopicsState.__new__(FeedTopicsState)
    state.send = AsyncMock()
    return state


@pytest.mark.asyncio
async def test_feed_command_entry_keeps_paused_feed_paused():
    active_week = {"id": 1, "status": "active", "accepted_topics": ["Роли"]}

    with patch("states.feed.topics.get_current_feed_week", new=AsyncMock(return_value=active_week)), \
         patch("states.feed.topics.update_intern", new=AsyncMock()) as update_intern:
        event = await make_topics_state().enter(make_intern(FeedStatus.PAUSED))

    assert event == "topics_selected"  # reader still reaches the digest
    update_intern.assert_not_awaited()


@pytest.mark.asyncio
async def test_feed_command_entry_still_starts_a_new_feed():
    active_week = {"id": 1, "status": "active", "accepted_topics": ["Роли"]}

    with patch("states.feed.topics.get_current_feed_week", new=AsyncMock(return_value=active_week)), \
         patch("states.feed.topics.update_intern", new=AsyncMock()) as update_intern:
        await make_topics_state().enter(make_intern(FeedStatus.NOT_STARTED, marathon_status="not_started"))

    update_intern.assert_awaited_once_with(CHAT_ID, mode="feed", feed_status=FeedStatus.ACTIVE)


# =============================================================================
# Scheduler: pause during generation cancels today's notification
# =============================================================================

@pytest.mark.asyncio
async def test_scheduler_skips_notification_when_paused_during_generation():
    bot = MagicMock(send_message=AsyncMock())
    week = {"id": 1, "status": "active", "accepted_topics": ["Роли"], "current_day": 2}
    readers = [make_intern(FeedStatus.ACTIVE), make_intern(FeedStatus.PAUSED)]  # before / after generation

    with patch("core.scheduler.get_intern", new=AsyncMock(side_effect=readers)), \
         patch("core.scheduler.get_current_feed_week", new=AsyncMock(return_value=week)), \
         patch("core.scheduler.expire_old_feed_sessions", new=AsyncMock()), \
         patch("core.scheduler.get_feed_session", new=AsyncMock(side_effect=[None, {"status": "pending"}])), \
         patch("core.scheduler.create_feed_session", new=AsyncMock(return_value={"id": 9})), \
         patch("engines.feed.planner.generate_multi_topic_digest",
               new=AsyncMock(return_value={"main_content": "текст"})), \
         patch("db.queries.notifications.try_insert_notification", new=AsyncMock(return_value=True)) as claim:
        await pre_generate_feed_digest(CHAT_ID, bot)

    claim.assert_not_awaited()
    bot.send_message.assert_not_awaited()


def test_pause_toasts_do_not_override_existing_feed_texts():
    # the legacy "feed is already active" text must keep its own wording
    assert t("feed.already_active", "ru") != t("feed.pause_already_active", "ru")
    for lang in ("ru", "en", "es", "fr", "zh"):
        for key in ("feed.pause_already_paused", "feed.pause_already_active", "feed.paused_notice"):
            assert t(key, lang) != key, (lang, key)
