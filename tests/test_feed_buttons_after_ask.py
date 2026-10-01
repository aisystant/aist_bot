"""Unit-тесты РП-498 Ф17: кнопки дайджеста после вопроса в Ленте.

Свободный текст Ленты теперь уходит в консультацию, и читатель остаётся в её состоянии. Кнопки старого
дайджеста («Фиксация», «Подробнее о теме») раньше в таком состоянии отправляли его в else-ветку
cb_feed_actions: дайджест присылался заново, а само нажатие терялось; следующий текст фиксации уходил в
консультацию как вопрос. Теперь кнопка тихо возвращает в Ленту и выполняется как обычно.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from core.machine import StateMachine  # noqa: E402
from handlers.callbacks import cb_feed_actions  # noqa: E402
from states.base import BaseState  # noqa: E402
from states.feed.digest import FeedDigestState  # noqa: E402

CHAT_ID = 6160
DIGEST = "feed.digest"
CONSULTATION = "common.consultation"


def make_callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.message.chat.id = CHAT_ID
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def make_intern(current_state: str) -> dict:
    return {"chat_id": CHAT_ID, "language": "ru", "current_state": current_state, "current_context": {}}


def make_dispatcher() -> MagicMock:
    return MagicMock(is_sm_active=True, go_to=AsyncMock(), route_callback=AsyncMock())


# =============================================================================
# Ветвление cb_feed_actions по текущему состоянию
# =============================================================================

@pytest.mark.asyncio
async def test_digest_button_in_consultation_returns_to_feed_silently_and_runs_the_action():
    callback = make_callback("feed_fixation")
    before, after = make_intern(CONSULTATION), make_intern(DIGEST)
    dispatcher = make_dispatcher()

    with patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("handlers.callbacks.get_intern", new=AsyncMock(side_effect=[before, after])):
        await cb_feed_actions(callback, AsyncMock())

    dispatcher.go_to.assert_awaited_once_with(before, DIGEST, context={"consultation_complete": True})
    dispatcher.route_callback.assert_awaited_once_with(after, callback)  # действие выполняется уже в Ленте
    callback.message.edit_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_digest_button_in_feed_goes_straight_to_the_state():
    callback = make_callback("feed_fixation")
    intern = make_intern(DIGEST)
    dispatcher = make_dispatcher()

    with patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("handlers.callbacks.get_intern", new=AsyncMock(return_value=intern)):
        await cb_feed_actions(callback, AsyncMock())

    dispatcher.route_callback.assert_awaited_once_with(intern, callback)
    dispatcher.go_to.assert_not_awaited()


@pytest.mark.asyncio
async def test_digest_button_in_another_state_still_reopens_the_digest():
    callback = make_callback("feed_fixation")
    intern = make_intern("common.mode_select")
    dispatcher = make_dispatcher()

    with patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("handlers.callbacks.get_intern", new=AsyncMock(return_value=intern)):
        await cb_feed_actions(callback, AsyncMock())

    callback.message.edit_reply_markup.assert_awaited_once()
    dispatcher.go_to.assert_awaited_once_with(intern, DIGEST)
    dispatcher.route_callback.assert_not_awaited()


# =============================================================================
# Вся цепочка через настоящие StateMachine и FeedDigestState
# =============================================================================

class ConsultationProbe(BaseState):
    """Подставная консультация: вход ничего не делает."""

    name = CONSULTATION
    keeps_session_on_reentry = True

    def __init__(self):
        super().__init__(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())

    async def handle(self, user, message):
        return None


@pytest.fixture
def digest():
    state = FeedDigestState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
    FeedDigestState._user_data.pop(CHAT_ID, None)  # класс-уровневый словарь делится между тестами
    yield state
    FeedDigestState._user_data.pop(CHAT_ID, None)


@pytest.mark.asyncio
async def test_chain_fixation_button_after_a_question_starts_the_fixation(digest):
    consultation = ConsultationProbe()
    sm = StateMachine()
    sm.register_all([digest, consultation])
    sm._transitions = {DIGEST: {"events": {}}, CONSULTATION: {"events": {"done": "_previous"}}}
    db_state = {"current": CONSULTATION}  # состояние пользователя «в базе»: после вопроса в Ленте он в консультации

    async def fake_update_state(chat_id, state_name):
        db_state["current"] = state_name

    async def fake_get_intern(chat_id):
        return make_intern(db_state["current"])

    dispatcher = SimpleNamespace(
        is_sm_active=True, go_to=sm.go_to, route_callback=lambda user, cb: sm.handle_callback(user, cb),
    )
    callback = make_callback("feed_fixation")

    with patch("handlers.get_dispatcher", return_value=dispatcher), \
         patch("handlers.callbacks.get_intern", new=AsyncMock(side_effect=fake_get_intern)), \
         patch("db.queries.get_intern", new=AsyncMock(side_effect=fake_get_intern)), \
         patch("db.queries.update_user_state", new=AsyncMock(side_effect=fake_update_state)), \
         patch.object(digest, "enter", new=AsyncMock()) as digest_enter:
        await cb_feed_actions(callback, AsyncMock())

    assert db_state["current"] == DIGEST
    assert FeedDigestState.is_waiting_fixation(CHAT_ID)  # следующий текст сохранится как фиксация, не уйдёт вопросом
    callback.message.answer.assert_awaited_once()        # читатель видит приглашение написать фиксацию
    digest_enter.assert_not_awaited()                    # дайджест заново не присылается
