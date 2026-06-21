"""Smoke-тесты: hermes_router (WP-392 Ф3.1, WP-428 Ф7).

«Гермес»/«hermes» → внешний Hermes-рантайм (Nous Research) через hermes_chat для T3+.
Для T1/T2 — Проводник (Haiku, DP.SC.169, WP-349 Ф33).
Роутер выделен из fallback и регистрируется ДО external_session/fallback.
WP-428 Ф7: session_id threads Hermes history; /hermes_reset generates a new session_id.
"""

import re
import pytest
from unittest.mock import AsyncMock, MagicMock

from handlers.hermes import _is_hermes_message, _HERMES_SESSION_KEY, on_hermes_reset
from tests.smoke.factories import text_message


# ─── Фильтр ───

def test_filter_matches_hermes_prefix():
    for txt in ("Гермес, привет", "гермес статус", "Hermes hi", "  Гермес"):
        msg = text_message(txt, chat_id=1).message
        assert _is_hermes_message(msg) is True


def test_filter_rejects_non_hermes():
    for txt in ("привет", "/start", "что по WP-392", "Герметичность"):
        # «Герметичность» begins with «гермет», not «гермес» → False
        msg = text_message(txt, chat_id=1).message
        assert _is_hermes_message(msg) is (txt.lower().startswith(("гермес", "hermes")))


# ─── WP-428 Ф7: /hermes_reset ───

_SESSION_ID_RE = re.compile(r"^tg-\d+-reset-\d+$")


@pytest.mark.asyncio
async def test_hermes_reset_guard_no_from_user():
    """Anonymous message (from_user=None) must be silently ignored."""
    message = MagicMock()
    message.from_user = None
    state = AsyncMock()
    await on_hermes_reset(message, state)
    state.update_data.assert_not_called()
    message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_hermes_reset_writes_new_session_id():
    """Happy path: writes tg-{user_id}-reset-{ts} under _HERMES_SESSION_KEY."""
    message = MagicMock()
    message.from_user = MagicMock()
    message.from_user.id = 42
    message.answer = AsyncMock()
    state = AsyncMock()
    await on_hermes_reset(message, state)
    state.update_data.assert_awaited_once()
    call_kwargs = state.update_data.call_args[0][0]
    assert _HERMES_SESSION_KEY in call_kwargs
    new_sid = call_kwargs[_HERMES_SESSION_KEY]
    assert _SESSION_ID_RE.match(new_sid), f"Unexpected session_id format: {new_sid}"
    assert new_sid.startswith("tg-42-reset-")
    message.answer.assert_awaited_once()


def test_session_key_constant_stable():
    """FSM key name must stay stable (other components may depend on it)."""
    assert _HERMES_SESSION_KEY == "hermes_session_id"
