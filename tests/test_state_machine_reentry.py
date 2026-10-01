"""Unit-тесты РП-498 Ф17: самопереход go_to() и память диалога консультации.

go_to() в то же состояние вызывал exit() безусловно, а exit() консультации стирает историю
диалога: кнопки «Подробнее» и «Обратная связь» обнуляли память. Состояние, объявившее
keeps_session_on_reentry, при самопереходе теперь остаётся в сессии; остальные ведут себя как раньше.
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

from core.machine import StateMachine  # noqa: E402
from states.base import BaseState  # noqa: E402
from states.common.consultation import ConsultationState  # noqa: E402

CHAT_ID = 4242
CONSULTATION = "common.consultation"  # имя из _MODAL_STATES в go_to()
DIGEST = "feed.digest"


class ProbeState(BaseState):
    """Подставное состояние: enter()/exit() только считают вызовы."""

    def __init__(self, name: str, keeps_session: bool = False, exit_context: dict | None = None):
        super().__init__(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
        self.name = name
        self.keeps_session_on_reentry = keeps_session
        self.enter = AsyncMock(return_value=None)
        self.exit = AsyncMock(return_value=exit_context or {})

    async def handle(self, user, message):
        return None


def make_machine(*states: BaseState) -> StateMachine:
    sm = StateMachine()
    sm.register_all(list(states))
    sm._transitions = {CONSULTATION: {"events": {"done": "_previous"}}}
    return sm


def make_user(current_state: str) -> dict:
    return {"chat_id": CHAT_ID, "current_state": current_state, "current_context": {}}


@pytest.fixture
def update_state():
    """Подмена БД в go_to(): запись нового состояния (её вызовы отдаём тесту) и перечитывание пользователя."""
    with patch("db.queries.update_user_state", new=AsyncMock()) as mock_update_state, \
         patch("db.queries.get_intern", new=AsyncMock(side_effect=lambda chat_id: make_user(CONSULTATION))):
        yield mock_update_state


# =============================================================================
# T1-T3: exit() при самопереходе и обычных переходах
# =============================================================================

@pytest.mark.asyncio
async def test_self_transition_keeps_session_skips_exit(update_state):
    state = ProbeState(CONSULTATION, keeps_session=True, exit_context={"consultation_complete": True})
    sm = make_machine(state)

    await sm.go_to(make_user(CONSULTATION), CONSULTATION, context={"question": "q"})

    assert state.exit.await_count == 0
    assert state.enter.await_count == 1
    # exit() не вызван, поэтому его контекст (consultation_complete) в enter() не попадает
    assert state.enter.await_args.args[1] == {"question": "q"}
    update_state.assert_awaited_once_with(CHAT_ID, CONSULTATION)


@pytest.mark.asyncio
async def test_self_transition_without_flag_still_calls_exit(update_state):
    state = ProbeState(DIGEST, keeps_session=False, exit_context={"from_exit": 1})
    sm = make_machine(state)

    await sm.go_to(make_user(DIGEST), DIGEST, context={"question": "q"})

    assert state.exit.await_count == 1
    assert state.enter.await_count == 1
    assert state.enter.await_args.args[1] == {"question": "q", "from_exit": 1}


@pytest.mark.asyncio
async def test_transition_to_other_state_calls_exit_even_with_flag(update_state):
    consultation = ProbeState(CONSULTATION, keeps_session=True)
    other = ProbeState("common.mode_select")
    sm = make_machine(consultation, other)

    await sm.go_to(make_user(CONSULTATION), "common.mode_select")

    assert consultation.exit.await_count == 1
    assert other.enter.await_count == 1
    assert consultation.enter.await_count == 0


@pytest.mark.asyncio
async def test_flag_must_be_literal_true_not_a_truthy_stub(update_state):
    # Подставное состояние с MagicMock-атрибутом: truthy, но не True. Такое не считается объявлением.
    stub = MagicMock()
    stub.name = DIGEST
    stub.exit = AsyncMock(return_value={})
    stub.enter = AsyncMock(return_value=None)
    sm = StateMachine()
    sm.register(stub)

    await sm.go_to(make_user(DIGEST), DIGEST)

    assert stub.exit.await_count == 1


# =============================================================================
# T4: предыдущее состояние после самоперехода
# =============================================================================

@pytest.mark.asyncio
async def test_previous_state_survives_self_transition(update_state):
    consultation = ProbeState(CONSULTATION, keeps_session=True)
    digest = ProbeState(DIGEST)
    sm = make_machine(consultation, digest)

    await sm.go_to(make_user(DIGEST), CONSULTATION, context={"question": "q1"})   # Лента -> консультация
    await sm.go_to(make_user(CONSULTATION), CONSULTATION, context={"refinement": True})  # кнопка «Подробнее»

    assert sm.get_next_state(CONSULTATION, "done", CHAT_ID) == DIGEST
    assert digest.exit.await_count == 1        # из Ленты вышли один раз
    assert consultation.exit.await_count == 0  # из консультации в себя не выходили


# =============================================================================
# Объявления флага в реальных классах
# =============================================================================

def test_flag_declared_only_where_session_lives_between_reentries():
    assert BaseState.keeps_session_on_reentry is False
    assert ConsultationState.keeps_session_on_reentry is True
