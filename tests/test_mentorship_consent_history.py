"""
WP-578 Ф11 — /mentor_consent_history: отдельное от /mentor_consent согласие
на перенос уже написанного участником раньше (импорт из экспорта Telegram
Desktop), не только новых сообщений. Peer-сессия 2026-09-23 (Claude+Kimi+
Codex): одной кнопкой с /mentor_consent объединять нельзя — согласие на
будущее и на прошлое разной природы (инвариант таблицы consent_grant,
"Retroactive expansion запрещена").
"""

import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_private_message(from_user_id=100):
    from aiogram.types import Message, User, Chat

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = from_user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = from_user_id
    msg.chat.type = "private"
    msg.answer = AsyncMock()
    return msg


def _make_callback(data: str, *, from_user_id=100):
    from aiogram.types import CallbackQuery, User, Message, Chat

    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = MagicMock(spec=User)
    callback.from_user.id = from_user_id
    callback.message = MagicMock(spec=Message)
    callback.message.chat = MagicMock(spec=Chat)
    callback.message.chat.id = from_user_id
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_command_sends_history_specific_prompt():
    import handlers.mentorship as mentorship

    message = _make_private_message()
    await mentorship.cmd_mentor_consent_history(message)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "раньше" in text
    assert text == mentorship.HISTORY_CONSENT_PROMPT_TEXT
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert buttons == ["mentor_consent_history:accept", "mentor_consent_history:revoke"]


@pytest.mark.asyncio
async def test_accept_grants_only_the_history_scope_not_dm_or_group(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value="acc-1"))
    set_consent_grant = AsyncMock()
    monkeypatch.setattr(mentorship, "set_consent_grant", set_consent_grant)

    callback = _make_callback("mentor_consent_history:accept")
    await mentorship.cb_mentor_consent_history(callback)

    # Exactly one scope written -- unlike cb_mentor_consent, which loops over
    # _CONSENT_SCOPES for both dm and group. Mixing the future-facing and
    # past-facing scopes into one call is exactly what this command exists
    # to avoid.
    set_consent_grant.assert_awaited_once_with("acc-1", "mentor_archive_history", granted=True)
    callback.message.edit_text.assert_awaited_once_with("✅ Согласие на перенос прошлой переписки зафиксировано.")
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_sets_granted_false(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value="acc-1"))
    set_consent_grant = AsyncMock()
    monkeypatch.setattr(mentorship, "set_consent_grant", set_consent_grant)

    callback = _make_callback("mentor_consent_history:revoke")
    await mentorship.cb_mentor_consent_history(callback)

    set_consent_grant.assert_awaited_once_with("acc-1", "mentor_archive_history", granted=False)
    callback.message.edit_text.assert_awaited_once_with("Согласие отозвано.")


@pytest.mark.asyncio
async def test_unlinked_account_shows_alert_and_writes_nothing(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=None))
    set_consent_grant = AsyncMock()
    monkeypatch.setattr(mentorship, "set_consent_grant", set_consent_grant)

    callback = _make_callback("mentor_consent_history:accept")
    await mentorship.cb_mentor_consent_history(callback)

    set_consent_grant.assert_not_called()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("show_alert") is True
