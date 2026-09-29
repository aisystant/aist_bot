"""
WP-578 — голый текст в личке боту без команды и без пересылки: если у
наставника есть свежий «активный участник» (тот же словарь, что у DM-версии
/mentor_note, tests/test_mentorship_note_dm.py), бот предлагает сохранить
текст как заметку вместо того, чтобы молча пропустить его в общий fallback.

Живой дефект найден 28.09 (пир-сессия
2026-09-28-12-wp578-mentor-dm-not-archived): наставник дважды написал текст
боту в личку, ожидая сохранения в архив, ничего не сохранилось и не было
ответа.
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
from unittest.mock import AsyncMock

from tests.mentorship_helpers import make_dm_message

MENTOR_ID = "11111111-1111-1111-1111-111111111111"
PARTICIPANT_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _clear_state():
    import handlers.mentorship as mentorship

    mentorship._pending_notes.clear()
    mentorship._active_participant.clear()
    yield
    mentorship._pending_notes.clear()
    mentorship._active_participant.clear()


def _set_active_participant(mentorship, *, from_user_id=100, set_at=4900.0, stream_id="S1"):
    mentorship._active_participant[from_user_id] = mentorship._ActiveParticipant(
        stream_id=stream_id,
        participant_account_id=PARTICIPANT_ID,
        participant_name="Иван Иванов",
        set_at=set_at,
    )


@pytest.mark.asyncio
async def test_no_active_participant_skips_without_touching_message(monkeypatch):
    """Обычный участник (или наставник вне окна TTL) — сообщение не перехватывается,
    маршрутизация продолжается дальше (SkipHandler), никакого сетевого вызова."""
    import handlers.mentorship as mentorship
    from aiogram.dispatcher.event.bases import SkipHandler

    resolver = AsyncMock()
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", resolver)

    message = make_dm_message(from_user_id=999, text="просто личное сообщение боту")

    with pytest.raises(SkipHandler):
        await mentorship.on_mentor_dm_free_text(message)

    message.reply.assert_not_awaited()
    resolver.assert_not_awaited()  # дешёвая проверка первой — не должно быть сетевого вызова вовсе


@pytest.mark.asyncio
async def test_expired_active_participant_skips(monkeypatch):
    import handlers.mentorship as mentorship
    from aiogram.dispatcher.event.bases import SkipHandler

    monkeypatch.setattr(mentorship, "time", lambda: 99999.0)
    _set_active_participant(mentorship, set_at=0.0)

    message = make_dm_message(from_user_id=100, text="текст после истечения TTL")

    with pytest.raises(SkipHandler):
        await mentorship.on_mentor_dm_free_text(message)

    message.reply.assert_not_awaited()
    assert (100, 100) not in mentorship._pending_notes


@pytest.mark.asyncio
async def test_empty_text_skips(monkeypatch):
    import handlers.mentorship as mentorship
    from aiogram.dispatcher.event.bases import SkipHandler

    monkeypatch.setattr(mentorship, "time", lambda: 5000.0)
    _set_active_participant(mentorship)

    message = make_dm_message(from_user_id=100, text="   ")

    with pytest.raises(SkipHandler):
        await mentorship.on_mentor_dm_free_text(message)

    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_mentor_account_unresolved_skips(monkeypatch):
    import handlers.mentorship as mentorship
    from aiogram.dispatcher.event.bases import SkipHandler

    monkeypatch.setattr(mentorship, "time", lambda: 5000.0)
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=None))
    _set_active_participant(mentorship)

    message = make_dm_message(from_user_id=100, text="текст без привязанного аккаунта")

    with pytest.raises(SkipHandler):
        await mentorship.on_mentor_dm_free_text(message)

    message.reply.assert_not_awaited()
    assert (100, 100) not in mentorship._pending_notes


@pytest.mark.asyncio
async def test_active_participant_present_stages_pending_note(monkeypatch):
    """Основной случай: активный участник есть и свеж -> заметка ставится в
    ту же очередь подтверждения, что и /mentor_note (переиспользование, не
    дублирование пути записи)."""
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "time", lambda: 5000.0)
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    _set_active_participant(mentorship, stream_id="S2-2026.3-T")

    message = make_dm_message(from_user_id=100, text="черновик заметки про участника")

    await mentorship.on_mentor_dm_free_text(message)

    pending = mentorship._pending_notes[(100, 100)]
    assert pending.body == "черновик заметки про участника"
    assert pending.mentor_account_id == MENTOR_ID
    assert pending.participant_account_id == PARTICIPANT_ID
    assert pending.stream_id == "S2-2026.3-T"

    message.reply.assert_awaited_once()
    reply_text = message.reply.await_args.args[0]
    assert "Иван Иванов" in reply_text
    assert "черновик заметки про участника" in reply_text
