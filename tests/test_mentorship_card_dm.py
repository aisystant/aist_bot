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
from tests.mentorship_helpers import make_dm_message as _make_dm_message
from tests.mentorship_helpers import (
    make_forward_from_channel,
    make_forward_from_chat,
    make_forward_hidden_author,
)

MENTOR_ID = "11111111-1111-1111-1111-111111111111"
PARTICIPANT_ID = "22222222-2222-2222-2222-222222222222"

# Fixed text of the service (DS-MCP/mentorship-service/src/tools/get-participant-card.ts,
# PR #1 of 28.09 added the mentor-side incompleteness sentence). The service is the
# source of truth: this constant mirrors it, independent of the participant / list emptiness.
CORRESPONDENCE_NOTE = (
    "Строка в recentMetadataActivity — сообщение без сохранённого текста (участник не дал согласие "
    "или строка историческая), это не пустое сообщение. В обоих списках только сообщения, "
    "распознанные ботом как адресованные наставнику; пустые списки не доказывают, что участник не писал. "
    "Реплики наставника в архиве неполны: сохраняются только текстовые ответы через «Ответить» на "
    "сообщение участника (в группе; так же при загрузке истории), личная переписка вживую не видна, "
    "сбои записи теряют часть. Отсутствие реплики не значит, что участнику не ответили."
)


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
        "mentorSideCoverage": "partial",
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


@pytest.mark.asyncio
async def test_dm_card_hidden_author_forward_is_refused_not_answered_with_the_previous_participant(monkeypatch):
    """Live check 02.10: three forwards by different people, two with a hidden author, and the bot showed the
    card of the first one three times. A hidden author is not the mentor's own message: refuse and drop the remembered participant."""
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    monkeypatch.setattr(mentorship, "time", lambda: 5000.0)
    mentorship._active_participant[100] = mentorship._ActiveParticipant(
        stream_id="S2",
        participant_account_id=PARTICIPANT_ID,
        participant_name="Пётр Петров",
        set_at=4900.0,
    )
    get_card_mock = AsyncMock()
    monkeypatch.setattr(mentorship.mentorship_service, "get_participant_card", get_card_mock)
    message = _make_dm_message(reply_to_message=make_forward_hidden_author("Анна К."), from_user_id=100)

    await mentorship.cmd_mentor_card_dm(message)

    get_card_mock.assert_not_awaited()
    reply_text = message.reply.await_args.args[0]
    assert "«Анна К.»" in reply_text
    assert "скрыл аккаунт" in reply_text
    assert "группе потока" in reply_text
    assert "Петров" not in reply_text
    assert 100 not in mentorship._active_participant


@pytest.mark.asyncio
@pytest.mark.parametrize("make_target", [make_forward_from_channel, make_forward_from_chat])
async def test_dm_card_forward_from_a_channel_or_chat_is_not_a_participant(monkeypatch, make_target):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    monkeypatch.setattr(mentorship, "time", lambda: 5000.0)
    mentorship._active_participant[100] = mentorship._ActiveParticipant(
        stream_id="S2", participant_account_id=PARTICIPANT_ID, participant_name="Пётр Петров", set_at=4900.0
    )
    get_card_mock = AsyncMock()
    monkeypatch.setattr(mentorship.mentorship_service, "get_participant_card", get_card_mock)
    message = _make_dm_message(reply_to_message=make_target(), from_user_id=100)

    await mentorship.cmd_mentor_card_dm(message)

    get_card_mock.assert_not_awaited()
    assert "не от участника" in message.reply.await_args.args[0]
    assert 100 not in mentorship._active_participant


@pytest.mark.asyncio
async def test_dm_card_forward_sequence_visible_hidden_visible_never_mixes_participants(monkeypatch):
    """Visible author A shows the card of A; a hidden author in between gets a refusal, not the card of A;
    the next visible author C switches the active participant to C."""
    import handlers.mentorship as mentorship

    a_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    c_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    # per command: the caller; for a visible author also the participant and the caller again (reader check)
    monkeypatch.setattr(
        mentorship,
        "resolve_ory_id_from_chat",
        AsyncMock(side_effect=[MENTOR_ID, a_id, MENTOR_ID, MENTOR_ID, MENTOR_ID, c_id, MENTOR_ID]),
    )
    monkeypatch.setattr(
        mentorship,
        "lookup_participant_stream",
        AsyncMock(
            side_effect=[
                StreamChatContext(stream_id="S1", reader_account_id=MENTOR_ID),
                StreamChatContext(stream_id="S2", reader_account_id=MENTOR_ID),
            ]
        ),
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))
    card = {"manualMinimum": {}, "correspondenceEmpty": True, "correspondenceNote": CORRESPONDENCE_NOTE, "recentNotes": []}
    get_card_mock = AsyncMock(return_value=card)
    monkeypatch.setattr(mentorship.mentorship_service, "get_participant_card", get_card_mock)

    first = _make_dm_message(reply_to_message=_make_forwarded_from_participant(201, full_name="Участник А"), from_user_id=100)
    await mentorship.cmd_mentor_card_dm(first)
    hidden = _make_dm_message(reply_to_message=make_forward_hidden_author("Скрытая Б"), from_user_id=100)
    await mentorship.cmd_mentor_card_dm(hidden)
    assert 100 not in mentorship._active_participant
    third = _make_dm_message(reply_to_message=_make_forwarded_from_participant(203, full_name="Участник В"), from_user_id=100)
    await mentorship.cmd_mentor_card_dm(third)

    assert [c.args for c in get_card_mock.await_args_list] == [(MENTOR_ID, "S1", a_id), (MENTOR_ID, "S2", c_id)]
    assert "Участник А" in first.reply.await_args.args[0]
    assert "скрыл аккаунт" in hidden.reply.await_args.args[0]
    assert "Участник А" not in hidden.reply.await_args.args[0]
    assert "Участник В" in third.reply.await_args.args[0]
    assert mentorship._active_participant[100].participant_account_id == c_id


@pytest.mark.asyncio
async def test_dm_card_plain_reply_with_active_participant_still_uses_it(monkeypatch):
    """Not a forward at all (the mentor replies to his own typed text): no other author is claimed, so the
    active participant stays the target. Only a forward whose author is hidden or not a user is refused."""
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock(return_value=MENTOR_ID))
    monkeypatch.setattr(mentorship, "time", lambda: 5000.0)
    mentorship._active_participant[100] = mentorship._ActiveParticipant(
        stream_id="S1", participant_account_id=PARTICIPANT_ID, participant_name="Иван Иванов", set_at=4900.0
    )
    card = {"manualMinimum": {}, "correspondenceEmpty": True, "correspondenceNote": CORRESPONDENCE_NOTE, "recentNotes": []}
    get_card_mock = AsyncMock(return_value=card)
    monkeypatch.setattr(mentorship.mentorship_service, "get_participant_card", get_card_mock)
    message = _make_dm_message(reply_to_message=_make_plain_reply(), from_user_id=100)

    await mentorship.cmd_mentor_card_dm(message)

    get_card_mock.assert_awaited_once_with(MENTOR_ID, "S1", PARTICIPANT_ID)
