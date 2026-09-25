"""
WP-578 Ф3 — /mentor_card в личке с ботом: наставник пересылает сюда сообщение
(участника или своё собственное) и отвечает на него командой — тот же
резолвер участника, что у DM-версии /mentor_note
(tests/test_mentorship_note_dm.py), только вместо сохранения заметки бот
показывает карточку.
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

from db.queries.mentorship import StreamChatContext

MENTOR_ID = "11111111-1111-1111-1111-111111111111"
PARTICIPANT_ID = "22222222-2222-2222-2222-222222222222"

# Реальный фиксированный текст сервиса (DS-MCP/mentorship-service/src/tools/
# get-participant-card.ts) — не зависит от участника/пустоты списков.
CORRESPONDENCE_NOTE = (
    "Строка в recentMetadataActivity — сообщение без сохранённого текста (участник не дал согласие "
    "или строка историческая), это не пустое сообщение. В обоих списках только сообщения, "
    "распознанные ботом как адресованные наставнику; пустые списки не доказывают, что участник не писал."
)


def _make_dm_message(*, reply_to_message=None, from_user_id=100):
    from aiogram.types import Message, User, Chat

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = from_user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = from_user_id  # личка: chat.id совпадает с user id
    msg.chat.type = "private"
    msg.reply_to_message = reply_to_message
    msg.reply = AsyncMock()
    return msg


def _make_forwarded_from_participant(participant_user_id: int, *, full_name: str = "Иван Иванов"):
    from aiogram.types import Message, User

    origin_user = MagicMock(spec=User)
    origin_user.id = participant_user_id
    origin_user.full_name = full_name

    origin = MagicMock()
    origin.sender_user = origin_user

    target = MagicMock(spec=Message)
    target.text = "вопрос от участника"
    target.caption = None
    target.forward_origin = origin
    return target


def _make_forwarded_own_message(mentor_user_id: int):
    from aiogram.types import Message, User

    origin_user = MagicMock(spec=User)
    origin_user.id = mentor_user_id

    origin = MagicMock()
    origin.sender_user = origin_user

    target = MagicMock(spec=Message)
    target.text = "мой старый ответ участнику"
    target.caption = None
    target.forward_origin = origin
    return target


def _make_plain_reply():
    from aiogram.types import Message

    target = MagicMock(spec=Message)
    target.text = "просто текст, не форвард"
    target.caption = None
    target.forward_origin = None
    return target


@pytest.fixture(autouse=True)
def _clear_state():
    import handlers.mentorship as mentorship

    mentorship._active_participant.clear()
    yield
    mentorship._active_participant.clear()


@pytest.mark.asyncio
async def test_dm_card_without_reply_target_asks_to_forward():
    import handlers.mentorship as mentorship

    message = _make_dm_message(reply_to_message=None)
    await mentorship.cmd_mentor_card_dm(message)

    message.reply.assert_awaited_once()
    assert "перешли" in message.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_dm_card_caller_without_account_rejected(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=None))
    target = _make_forwarded_from_participant(200)
    message = _make_dm_message(reply_to_message=target)

    await mentorship.cmd_mentor_card_dm(message)

    message.reply.assert_awaited_once_with("Не нашёл твой аккаунт платформы — сначала привяжи его (/link).")


@pytest.mark.asyncio
async def test_dm_card_forward_from_participant_shows_card(monkeypatch):
    import handlers.mentorship as mentorship

    # Порядок вызовов resolve_ory_id_from_chat: (1) наставник — проверка
    # аккаунта в самой команде, (2) внутри резолвера — участник, (3) внутри
    # резолвера — наставник ещё раз для get_stream_reader_role.
    monkeypatch.setattr(
        mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=[MENTOR_ID, PARTICIPANT_ID, MENTOR_ID])
    )
    monkeypatch.setattr(
        mentorship, "lookup_participant_stream", AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id=MENTOR_ID))
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))
    card = {"manualMinimum": {}, "correspondenceEmpty": True, "correspondenceNote": CORRESPONDENCE_NOTE, "recentNotes": []}
    get_card_mock = AsyncMock(return_value=card)
    monkeypatch.setattr(mentorship.mentorship_service, "get_participant_card", get_card_mock)

    target = _make_forwarded_from_participant(200, full_name="Иван Иванов")
    message = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_card_dm(message)

    get_card_mock.assert_awaited_once_with(MENTOR_ID, "S1", PARTICIPANT_ID)
    reply_text = message.reply.await_args.args[0]
    assert "Иван Иванов" in reply_text
    assert "пока нет сообщений" in reply_text
    assert CORRESPONDENCE_NOTE in reply_text

    # forward от участника обновляет «активного участника» на будущее, как у /mentor_note
    active = mentorship._active_participant[100]
    assert active.participant_account_id == PARTICIPANT_ID


@pytest.mark.asyncio
async def test_dm_card_own_forward_uses_active_participant(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    monkeypatch.setattr(mentorship, "time", lambda: 5000.0)
    mentorship._active_participant[100] = mentorship._ActiveParticipant(
        stream_id="S1",
        participant_account_id=PARTICIPANT_ID,
        participant_name="Иван Иванов",
        set_at=4900.0,
    )
    card = {"manualMinimum": {}, "correspondenceEmpty": True, "correspondenceNote": CORRESPONDENCE_NOTE, "recentNotes": []}
    get_card_mock = AsyncMock(return_value=card)
    monkeypatch.setattr(mentorship.mentorship_service, "get_participant_card", get_card_mock)

    target = _make_forwarded_own_message(100)
    message = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_card_dm(message)

    get_card_mock.assert_awaited_once_with(MENTOR_ID, "S1", PARTICIPANT_ID)


def test_format_participant_card_shows_note_even_with_non_empty_correspondence():
    """Регрессия против Critical-находки первого ревью (25.09): сервис
    (DS-MCP/mentorship-service) отдаёт correspondenceNote БЕЗУСЛОВНО, не
    только при пустой переписке — предупреждение обязано появляться и в
    непустом случае, не только в _format_participant_card, вызванном для
    группы (tests/test_mentorship_card.py), но и здесь: функция общая для
    группы и личики, второй регрессии по тому же коду быть не должно."""
    import handlers.mentorship as mentorship

    card = {
        "manualMinimum": {},
        "correspondenceEmpty": False,
        "recentTextCorrespondence": [{"author": "participant", "text": "привет"}],
        "recentMetadataActivity": [],
        "recentNotes": [],
        "correspondenceNote": CORRESPONDENCE_NOTE,
    }
    text = mentorship._format_participant_card(card, "Иван Иванов", "S1")

    assert CORRESPONDENCE_NOTE in text


@pytest.mark.asyncio
async def test_dm_card_plain_reply_without_active_participant_asks_for_participant_forward_first(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    target = _make_plain_reply()
    message = _make_dm_message(reply_to_message=target, from_user_id=100)

    await mentorship.cmd_mentor_card_dm(message)

    reply_text = message.reply.await_args.args[0]
    assert "не могу понять" in reply_text.lower()
