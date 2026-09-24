"""
WP-578 — cb_mentor_consent (пир-сессия 24.09, Claude+Kimi+Codex): фикс гонки
кнопки согласия. Общее сообщение с кнопкой используется и в личке, и в группе
(дисклеймер потока, DRR-f2 §5) — раньше первый клик через edit_text() снимал
клавиатуру у ОБЩЕГО сообщения для всех участников, хотя согласие пишется
per-account_id и каждый должен иметь возможность кликнуть сам.
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


def _make_callback(*, data: str, from_user_id: int = 100):
    from aiogram.types import CallbackQuery, Message, User

    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = MagicMock(spec=User)
    callback.from_user.id = from_user_id
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_consent_accept_does_not_edit_shared_message(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value="account-1"))
    set_consent_grant = AsyncMock()
    monkeypatch.setattr(mentorship, "set_consent_grant", set_consent_grant)

    callback = _make_callback(data="mentor_consent:accept")

    await mentorship.cb_mentor_consent(callback)

    callback.message.edit_text.assert_not_called()
    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.await_args
    assert "зафиксировано" in args[0]
    assert kwargs.get("show_alert") is True
    assert set_consent_grant.await_count == len(mentorship._CONSENT_SCOPES)


@pytest.mark.asyncio
async def test_consent_revoke_does_not_edit_shared_message(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value="account-1"))
    monkeypatch.setattr(mentorship, "set_consent_grant", AsyncMock())

    callback = _make_callback(data="mentor_consent:revoke")

    await mentorship.cb_mentor_consent(callback)

    callback.message.edit_text.assert_not_called()
    args, kwargs = callback.answer.await_args
    assert "отозвано" in args[0]
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_consent_second_click_from_different_account_still_recorded(monkeypatch):
    """Регрессия самого бага: второй участник кликает то же общее сообщение
    после первого — раньше кнопка/текст были бы уже стёрты первым кликом,
    теперь сообщение не трогается вовсе, и второй клик просто работает."""
    import handlers.mentorship as mentorship

    monkeypatch.setattr(
        mentorship,
        "resolve_ory_id_from_chat",
        AsyncMock(side_effect=["account-1", "account-2"]),
    )
    set_consent_grant = AsyncMock()
    monkeypatch.setattr(mentorship, "set_consent_grant", set_consent_grant)

    shared_message = MagicMock()
    shared_message.edit_text = AsyncMock()

    first_click = _make_callback(data="mentor_consent:accept", from_user_id=1)
    first_click.message = shared_message
    second_click = _make_callback(data="mentor_consent:accept", from_user_id=2)
    second_click.message = shared_message

    await mentorship.cb_mentor_consent(first_click)
    await mentorship.cb_mentor_consent(second_click)

    shared_message.edit_text.assert_not_called()
    first_click.answer.assert_awaited_once()
    second_click.answer.assert_awaited_once()
    assert set_consent_grant.await_count == 2 * len(mentorship._CONSENT_SCOPES)
