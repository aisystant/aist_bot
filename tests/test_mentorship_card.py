"""
WP-578 Ф3 — /mentor_card (группа): наставник отвечает на сообщение участника,
бот вызывает get_participant_card mentorship-service и показывает срез
(переписка/заметки/внешние источники). Бот = тонкий клиент, сам карточку не
считает — только форматирует готовый ответ сервиса.

Карточка несёт приватные данные участника — уходит наставнику личным
сообщением, в группе остаётся только короткое подтверждение без содержимого.

F9: usage errors go to the mentor's DM, not the group (silence for an outsider
is covered by test_mentorship_silence.py, "card" case).
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

from tests.mentorship_helpers import assert_only_dm_to_mentor, as_stream_reader
from clients.mentorship_service import MentorshipServiceError

MENTOR_TG_ID = 100

# Реальный фиксированный текст сервиса (DS-MCP/mentorship-service/src/tools/
# get-participant-card.ts) — не зависит от участника/пустоты списков.
CORRESPONDENCE_NOTE = (
    "Строка в recentMetadataActivity — сообщение без сохранённого текста (участник не дал согласие "
    "или строка историческая), это не пустое сообщение. В обоих списках только сообщения, "
    "распознанные ботом как адресованные наставнику; пустые списки не доказывают, что участник не писал."
)


def _make_message(*, reply_to_message=None, from_user_id=MENTOR_TG_ID):
    from aiogram.types import Message, User, Chat

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = from_user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = -1001
    msg.chat.type = "group"
    msg.reply_to_message = reply_to_message
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.bot = AsyncMock()
    return msg


def _make_target_user(user_id: int, full_name: str = "Участник Тестов"):
    from aiogram.types import Message, User

    target = MagicMock(spec=Message)
    target.from_user = MagicMock(spec=User)
    target.from_user.id = user_id
    target.from_user.full_name = full_name
    return target


@pytest.mark.asyncio
async def test_card_without_reply_target_tells_mentor_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")])
    message = _make_message(reply_to_message=None)

    await mentorship.cmd_mentor_card(message)

    assert_only_dm_to_mentor(message, "Ответь этой командой на сообщение участника, чью карточку нужно показать.")


@pytest.mark.asyncio
async def test_card_self_is_rejected_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")])
    target = _make_target_user(MENTOR_TG_ID)  # тот же id, что и у наставника
    message = _make_message(reply_to_message=target)

    await mentorship.cmd_mentor_card(message)

    assert_only_dm_to_mentor(message, "Нельзя посмотреть карточку самого себя.")


@pytest.mark.asyncio
async def test_card_reader_of_other_stream_is_told_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S2", "mentor")])
    message = _make_message(reply_to_message=_make_target_user(200))

    await mentorship.cmd_mentor_card(message)

    assert_only_dm_to_mentor(message, "не числишься наставником или пилотом потока S1")


@pytest.mark.asyncio
async def test_card_participant_without_account_is_told_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", None))
    message = _make_message(reply_to_message=_make_target_user(200))

    await mentorship.cmd_mentor_card(message)

    assert_only_dm_to_mentor(message, "У участника нет привязанного аккаунта платформы — карточка недоступна.")


@pytest.mark.asyncio
async def test_card_success_sends_card_by_dm_and_confirms_in_group(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", "participant-account"))
    card = {
        "manualMinimum": {"red_flag": "пропустил 2 занятия", "next_step": "созвониться"},
        "correspondenceEmpty": False,
        "recentTextCorrespondence": [
            {"author": "participant", "text": "застрял на задании 3"},
            {"author": "mentor", "text": "давай на созвон"},
        ],
        "recentMetadataActivity": [{}],
        "recentNotes": [{"body": "прогресс медленный, но стабильный"}],
        "correspondenceNote": CORRESPONDENCE_NOTE,
    }
    get_card_mock = AsyncMock(return_value=card)
    monkeypatch.setattr(mentorship.mentorship_service, "get_participant_card", get_card_mock)

    message = _make_message(reply_to_message=_make_target_user(200, full_name="Иван Иванов"))

    await mentorship.cmd_mentor_card(message)

    get_card_mock.assert_awaited_once_with("mentor-account", "S1", "participant-account")
    message.bot.send_message.assert_awaited_once()
    assert message.bot.send_message.await_args.args[0] == MENTOR_TG_ID
    dm_text = message.bot.send_message.await_args.args[1]
    assert "Иван Иванов" in dm_text
    assert "пропустил 2 занятия" in dm_text
    assert "созвониться" in dm_text
    assert "застрял на задании 3" in dm_text
    assert "давай на созвон" in dm_text
    assert "прогресс медленный, но стабильный" in dm_text
    assert "+1 сообщений без сохранённого текста" in dm_text
    # Регрессия против Critical-находки первого ревью (25.09): сервис отдаёт
    # correspondenceNote БЕЗУСЛОВНО (не только при пустой переписке).
    assert CORRESPONDENCE_NOTE in dm_text

    # В группе — только подтверждение без содержимого карточки, не утечка
    # приватных данных участника в общий чат.
    message.reply.assert_awaited_once_with("Карточка участника Иван Иванов отправлена тебе в личку.")
    assert "застрял на задании 3" not in message.reply.await_args.args[0]
    assert "пропустил 2 занятия" not in message.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_card_empty_correspondence_shows_fixed_text_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", "participant-account"))
    card = {"manualMinimum": {}, "correspondenceEmpty": True, "correspondenceNote": CORRESPONDENCE_NOTE, "recentNotes": []}
    monkeypatch.setattr(mentorship.mentorship_service, "get_participant_card", AsyncMock(return_value=card))

    message = _make_message(reply_to_message=_make_target_user(200))

    await mentorship.cmd_mentor_card(message)

    dm_text = message.bot.send_message.await_args.args[1]
    assert "пока нет сообщений" in dm_text
    assert CORRESPONDENCE_NOTE in dm_text


@pytest.mark.asyncio
async def test_card_service_unavailable_tells_mentor_privately(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", "participant-account"))
    monkeypatch.setattr(mentorship.mentorship_service, "get_participant_card", AsyncMock(return_value=None))

    message = _make_message(reply_to_message=_make_target_user(200))

    await mentorship.cmd_mentor_card(message)

    assert_only_dm_to_mentor(message, "недоступен")


@pytest.mark.asyncio
async def test_card_service_error_tells_mentor_privately_without_crashing(monkeypatch):
    import handlers.mentorship as mentorship

    as_stream_reader(monkeypatch, mentorship, streams=[("S1", "mentor")], account_ids=("mentor-account", "participant-account"))
    monkeypatch.setattr(
        mentorship.mentorship_service,
        "get_participant_card",
        AsyncMock(side_effect=MentorshipServiceError("access_denied", "вызывающий не наставник")),
    )

    message = _make_message(reply_to_message=_make_target_user(200))

    await mentorship.cmd_mentor_card(message)

    assert_only_dm_to_mentor(message, "Не удалось получить карточку")


def test_format_participant_card_truncates_long_text():
    import handlers.mentorship as mentorship

    card = {
        "manualMinimum": {},
        "correspondenceEmpty": False,
        "recentTextCorrespondence": [{"author": "participant", "text": "x" * 300}],
        "recentMetadataActivity": [],
        "recentNotes": [],
    }
    text = mentorship._format_participant_card(card, "Иван Иванов", "S1")

    assert "…" in text
    assert "x" * 300 not in text


def test_format_participant_card_truncates_long_note():
    """Регрессия против High-находки ревью 25.09: заметки наставника — тоже
    свободный текст произвольной длины, не только переписка."""
    import handlers.mentorship as mentorship

    card = {
        "manualMinimum": {},
        "correspondenceEmpty": True,
        "recentNotes": [{"body": "y" * 500}],
    }
    text = mentorship._format_participant_card(card, "Иван Иванов", "S1")

    assert "…" in text
    assert "y" * 500 not in text


def test_format_participant_card_caps_total_length_under_telegram_limit():
    """Регрессия против High-находки ревью 25.09: без общего лимита длинная
    карточка (много записей переписки и заметок) может превысить лимит
    Telegram sendMessage (4096 символов) и упасть без обработки на DM-пути."""
    import handlers.mentorship as mentorship

    card = {
        "manualMinimum": {},
        "correspondenceEmpty": False,
        "recentTextCorrespondence": [{"author": "participant", "text": "a" * 200} for _ in range(5)],
        "recentMetadataActivity": [],
        "recentNotes": [{"body": "b" * 200} for _ in range(5)],
        "correspondenceNote": "c" * 500,
    }
    text = mentorship._format_participant_card(card, "Иван Иванов", "S1")

    assert len(text) <= mentorship._CARD_MAX_CHARS + len("\n…(карточка обрезана, слишком длинная для одного сообщения)")
