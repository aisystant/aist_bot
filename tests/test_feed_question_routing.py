"""Unit-тесты РП-498 Ф17: свободный текст в Ленте уходит в консультацию.

Раньше текст без «?» в Ленте получал одноразовый ответ без памяти диалога: на реплику «опиши»
бот не помнил, о чём шла речь. Теперь FeedDigestState передаёт текст консультации (go_to), у которой
есть история, роли и кнопки. Фиксация, команды и режим без State Machine ведут себя как раньше;
флаг FEED_QUESTIONS_VIA_CONSULTATION=false возвращает прежний одноразовый ответ.
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
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from core.machine import StateMachine  # noqa: E402
from states.base import BaseState  # noqa: E402
from states.feed.digest import FeedDigestState  # noqa: E402

CHAT_ID = 5150
DIGEST = "feed.digest"
CONSULTATION = "common.consultation"
WEEK = {"id": 1, "accepted_topics": ["Внимание", "Собранность"]}


def make_digest() -> FeedDigestState:
    return FeedDigestState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())


def make_user() -> dict:
    return {"chat_id": CHAT_ID, "current_state": DIGEST, "language": "ru", "current_context": {}}


def text_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


@pytest.fixture
def digest():
    state = make_digest()
    FeedDigestState._user_data.pop(CHAT_ID, None)  # класс-уровневый словарь делится между тестами
    yield state
    FeedDigestState._user_data.pop(CHAT_ID, None)


@pytest.fixture
def dispatcher():
    """Подмена handlers.get_dispatcher: State Machine поднята, go_to ловит вызов."""
    stub = MagicMock(is_sm_active=True, go_to=AsyncMock())
    with patch("handlers.get_dispatcher", return_value=stub):
        yield stub


@pytest.fixture
def week():
    with patch("states.feed.digest.get_current_feed_week", new=AsyncMock(return_value=WEEK)):
        yield


# =============================================================================
# F1-F4: маршрутизация текста в handle()
# =============================================================================

@pytest.mark.asyncio
async def test_free_text_goes_to_consultation_with_week_topics(digest, dispatcher, week):
    user = make_user()

    with patch.object(digest, "_handle_question", new=AsyncMock()) as one_shot:
        result = await digest.handle(user, text_message("опиши"))

    assert result is None
    dispatcher.go_to.assert_awaited_once_with(
        user, CONSULTATION, context={"question": "опиши", "context_topic": "Внимание, Собранность"},
    )
    one_shot.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixation_text_is_not_a_question(digest, dispatcher, week):
    FeedDigestState._user_data[CHAT_ID] = {"waiting_fixation": True}

    with patch.object(digest, "_handle_fixation", new=AsyncMock(return_value="fixation_saved")) as fixation, \
         patch.object(digest, "_handle_question", new=AsyncMock()) as one_shot:
        result = await digest.handle(make_user(), text_message("Главное, что я понял: внимание можно тренировать"))

    assert result == "fixation_saved"
    fixation.assert_awaited_once()
    dispatcher.go_to.assert_not_awaited()
    one_shot.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_text_is_ignored(digest, dispatcher, week):
    with patch.object(digest, "_handle_question", new=AsyncMock()) as one_shot:
        result = await digest.handle(make_user(), text_message("/progress"))

    assert result is None
    dispatcher.go_to.assert_not_awaited()
    one_shot.assert_not_awaited()


@pytest.mark.asyncio
async def test_too_short_text_is_ignored_as_before(digest, dispatcher, week):
    with patch.object(digest, "_handle_question", new=AsyncMock()) as one_shot:
        await digest.handle(make_user(), text_message("да"))

    dispatcher.go_to.assert_not_awaited()
    one_shot.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_state_machine_falls_back_to_one_shot_answer(digest, week):
    user = make_user()

    with patch("handlers.get_dispatcher", return_value=None), \
         patch.object(digest, "_handle_question", new=AsyncMock()) as one_shot:
        await digest.handle(user, text_message("опиши"))

    one_shot.assert_awaited_once_with(user, "опиши")


@pytest.mark.asyncio
async def test_inactive_state_machine_falls_back_to_one_shot_answer(digest, week):
    user = make_user()
    inactive = MagicMock(is_sm_active=False, go_to=AsyncMock())

    with patch("handlers.get_dispatcher", return_value=inactive), \
         patch.object(digest, "_handle_question", new=AsyncMock()) as one_shot:
        await digest.handle(user, text_message("опиши"))

    one_shot.assert_awaited_once_with(user, "опиши")
    inactive.go_to.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_off_keeps_one_shot_answer(digest, dispatcher, week):
    user = make_user()

    with patch("states.feed.digest.FEED_QUESTIONS_VIA_CONSULTATION", False), \
         patch.object(digest, "_handle_question", new=AsyncMock()) as one_shot:
        await digest.handle(user, text_message("опиши"))

    one_shot.assert_awaited_once_with(user, "опиши")
    dispatcher.go_to.assert_not_awaited()


@pytest.mark.asyncio
async def test_week_without_topics_passes_no_topic_context(digest, dispatcher):
    user = make_user()

    with patch("states.feed.digest.get_current_feed_week", new=AsyncMock(return_value=None)):
        await digest.handle(user, text_message("опиши"))

    dispatcher.go_to.assert_awaited_once_with(
        user, CONSULTATION, context={"question": "опиши", "context_topic": None},
    )


# =============================================================================
# F5: вся цепочка через настоящие StateMachine и FeedDigestState
# =============================================================================

class ConsultationProbe(BaseState):
    """Подставная консультация: запоминает контекст входа."""

    name = CONSULTATION
    keeps_session_on_reentry = True

    def __init__(self):
        super().__init__(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
        self.enter = AsyncMock(return_value=None)

    async def handle(self, user, message):
        return None


@pytest.mark.asyncio
async def test_chain_feed_text_enters_consultation_and_remembers_feed_as_previous(digest, week):
    consultation = ConsultationProbe()
    sm = StateMachine()
    sm.register_all([digest, consultation])
    sm._transitions = {
        DIGEST: {"events": {}, "allow_global": ["consultation"]},
        CONSULTATION: {"events": {"done": "_previous"}},
    }
    sm._global_events = {"consultation": {"trigger": "?", "target": CONSULTATION}}
    user = make_user()
    bot_dispatcher = SimpleNamespace(is_sm_active=True, go_to=sm.go_to)

    with patch("handlers.get_dispatcher", return_value=bot_dispatcher), \
         patch("db.queries.update_user_state", new=AsyncMock()) as update_state, \
         patch("db.queries.get_intern", new=AsyncMock(return_value=user)), \
         patch.object(digest, "_handle_question", new=AsyncMock()) as one_shot:
        await sm.handle(user, text_message("опиши"))

    one_shot.assert_not_awaited()
    update_state.assert_awaited_once_with(CHAT_ID, CONSULTATION)
    consultation.enter.assert_awaited_once()
    assert consultation.enter.await_args.args[1] == {"question": "опиши", "context_topic": "Внимание, Собранность"}
    assert sm.get_next_state(CONSULTATION, "done", CHAT_ID) == DIGEST  # возврат из консультации — в Ленту
