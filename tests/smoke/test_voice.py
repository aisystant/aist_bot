"""Smoke tests: voice_router (WP-384, DP.SC.178).

Voice message -> speech-to-text -> echo -> the path typed text would take: a ".text"
fleeting note by default (marked as spoken, with an undo button), «заметка …» forces a note,
«вопрос …» asks the mentor ("?" question). A voice message sent before the consent is
processed right after the user agrees.
"""

import asyncio
import io
import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from clients.github_api import NoteTarget, RemoveStatus, github_notes
from clients.github_auth import GitHubAuthUnavailable
from core.fleeting_notes import NoteOutcome, NoteSource, NoteStatus
from db.queries.voice_operations import ClaimState, VoiceClaim
from handlers.voice import (
    TranscriptionError,
    cmd_voice_off,
    on_note_undo,
    on_voice,
    on_voice_consent_grant,
    split_spoken_note,
    split_spoken_question,
    voice_claimable,
)
from handlers.voice import (
    _fsm_is_clear as real_fsm_is_clear,
)


@pytest.fixture(autouse=True)
def _fsm_clear():
    with patch("handlers.voice._fsm_is_clear", AsyncMock(return_value=True)) as clear:
        yield clear


@pytest.fixture(autouse=True)
def _operation_storage():
    claim = VoiceClaim(ClaimState.CLAIMED, uuid4(), uuid4())
    with (
        patch("handlers.voice.voice_enabled_for", return_value=True),
        patch(
            "handlers.voice.voice_ops.claim_voice_operation",
            AsyncMock(return_value=claim),
        ) as acquire,
        patch(
            "handlers.voice.voice_ops.renew_voice_operation",
            AsyncMock(return_value=True),
        ) as renew,
        patch(
            "handlers.voice.voice_ops.release_voice_operation",
            AsyncMock(return_value=True),
        ) as release,
        patch(
            "handlers.voice.voice_ops.complete_voice_operation", AsyncMock()
        ) as complete,
        patch(
            "handlers.voice.voice_ops.bind_voice_kind",
            AsyncMock(side_effect=lambda claim, kind: kind),
        ),
        patch(
            "handlers.voice.voice_ops.bind_voice_note_hash",
            AsyncMock(side_effect=lambda _claim, digest: digest),
        ),
        patch(
            "handlers.voice.voice_ops.bind_voice_target",
            AsyncMock(side_effect=lambda claim, target: target),
        ),
        patch(
            "handlers.voice.github_notes.resolve_target",
            AsyncMock(return_value=NoteTarget("owner/notes", "notes.md", "main")),
        ),
    ):
        yield SimpleNamespace(
            claim=claim,
            acquire=acquire,
            renew=renew,
            release=release,
            complete=complete,
        )


@pytest.fixture(autouse=True)
def _bot_not_waiting_for_reply():
    with patch(
        "handlers.voice._bot_awaits_reply", new_callable=AsyncMock, return_value=False
    ) as waiting:
        yield waiting


def _voice_message(duration: int = 10, chat_type: str = "private") -> MagicMock:
    """Minimal Message mock carrying a voice message."""
    msg = MagicMock()
    msg.chat.id = 12345
    msg.chat.type = chat_type
    msg.from_user.id = 12345
    msg.message_id = 51
    msg.reply_to_message = None
    msg.voice.duration = duration
    msg.voice.file_id = "voice_file_id"
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.id = 99
    msg.bot.download = AsyncMock(return_value=io.BytesIO(b"fake-ogg-bytes"))
    msg.model_copy = MagicMock(
        side_effect=lambda update: MagicMock(text=update["text"], voice=update["voice"])
    )
    return msg


@contextmanager
def _ready_user(transcript="купить молоко", routed=True):
    """Account linked, consent given, STT configured; yields the patched collaborators."""
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value="ory-uuid-123",
        ),
        patch(
            "handlers.voice.get_consent_grant",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("handlers.voice.STT_API_KEY", "stt-test"),
        patch(
            "handlers.voice._transcribe_voice",
            new_callable=AsyncMock,
            return_value=transcript,
        ) as stt,
        patch(
            "handlers.github.process_fleeting_note",
            new_callable=AsyncMock,
            return_value=NoteOutcome(NoteStatus.SAVED, repo="owner/notes"),
        ) as note,
        patch(
            "handlers.voice._ask_as_question",
            new_callable=AsyncMock,
            return_value=routed,
        ) as ask,
    ):
        yield stt, note, ask


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Вопрос: что у меня в плане?", "что у меня в плане?"),
        ("вопрос, как начать марафон", "как начать марафон"),
        ("ВОПРОС — где мои заметки", "где мои заметки"),
        ("вопрос", None),
        ("вопросы стратегии на декабрь", None),
        ("идея про экзоскелет мышления", None),
    ],
)
def test_split_spoken_question(transcript, expected):
    assert split_spoken_question(transcript) == expected


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Заметка: вопрос к Андрею про Neon", "вопрос к Андрею про Neon"),
        ("заметка, купить молоко", "купить молоко"),
        ("заметка", None),
        ("заметки со встречи", None),
        ("купить молоко", None),
    ],
)
def test_split_spoken_note(transcript, expected):
    assert split_spoken_note(transcript) == expected


def _reply_to(is_bot: bool) -> MagicMock:
    replied = MagicMock()
    replied.from_user.is_bot = is_bot
    return replied


@pytest.mark.parametrize(
    ("chat_type", "reply_to", "allowed", "expected"),
    [
        ("private", None, True, True),
        ("private", "own", True, True),
        ("private", "bot", True, False),
        ("private", None, False, False),
        ("supergroup", None, True, False),
    ],
)
def test_voice_claimable(chat_type, reply_to, allowed, expected):
    msg = _voice_message(chat_type=chat_type)
    msg.reply_to_message = (
        None if reply_to is None else _reply_to(is_bot=reply_to == "bot")
    )
    with patch("handlers.voice.voice_enabled_for", return_value=allowed) as enabled:
        assert voice_claimable(msg) is expected
    if chat_type == "private" and reply_to != "bot":
        enabled.assert_called_once_with(12345)


@pytest.mark.asyncio
async def test_voice_skipped_when_bot_awaits_reply(_bot_not_waiting_for_reply):
    _bot_not_waiting_for_reply.return_value = True
    msg = _voice_message()
    with pytest.raises(SkipHandler):
        await on_voice(msg)
    msg.answer.assert_not_awaited()
    msg.bot.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_rejects_too_long():
    msg = _voice_message(duration=120)
    await on_voice(msg)
    msg.answer.assert_awaited_once()
    assert "длиннее" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_voice_requires_account():
    msg = _voice_message()
    with patch(
        "handlers.voice.resolve_ory_id_from_chat",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await on_voice(msg)
    msg.answer.assert_awaited_once()
    assert "Подключить аккаунт" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_voice_requires_consent_asked_as_a_reply_with_honest_text():
    msg = _voice_message()
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value="ory-uuid-123",
        ),
        patch(
            "handlers.voice.get_consent_grant",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await on_voice(msg)
    msg.reply.assert_awaited_once()
    msg.answer.assert_not_awaited()
    args, kwargs = msg.reply.call_args
    text = args[0]
    assert "согласие" in text.lower()
    assert "OpenRouter" in text and "правилам хранения" in text
    assert "копия текста заметки" in text
    assert "/voice_off" in text
    assert (
        kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        == "voice_consent_grant"
    )
    msg.bot.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_default_is_a_voice_marked_dot_note():
    msg = _voice_message()
    with _ready_user(transcript="купить молоко") as (stt, note, ask):
        await on_voice(msg)

    stt.assert_awaited_once_with(b"fake-ogg-bytes")
    assert msg.answer.call_args_list[0][0][0] == "🎤 купить молоко"
    note.assert_awaited_once()
    typed, source = note.call_args[0]
    assert typed.text == ".купить молоко"
    assert typed.voice is None
    assert source is NoteSource.VOICE
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_spoken_note_prefix_is_removed_and_keeps_a_question_word_in_the_note():
    msg = _voice_message()
    with _ready_user(transcript="Заметка: вопрос к Андрею про Neon") as (
        _stt,
        note,
        ask,
    ):
        await on_voice(msg)

    typed, source = note.call_args[0]
    assert typed.text == ".вопрос к Андрею про Neon"
    assert source is NoteSource.VOICE
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_starting_with_question_word_asks_mentor():
    msg = _voice_message()
    with _ready_user(transcript="Вопрос: как начать марафон") as (_stt, note, ask):
        await on_voice(msg)

    ask.assert_awaited_once_with(msg, "как начать марафон")
    note.assert_not_awaited()
    assert msg.answer.await_count == 1


@pytest.mark.asyncio
async def test_voice_question_not_routed_tells_user_to_type():
    msg = _voice_message()
    with _ready_user(transcript="вопрос где мои заметки", routed=False):
        await on_voice(msg)
    assert "Ответить сейчас" in msg.reply.call_args[0][0]
    assert (
        msg.reply.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        == "voice_retry"
    )


@pytest.mark.asyncio
async def test_voice_transcription_failure_falls_back_to_text():
    msg = _voice_message()
    with _ready_user() as (stt, note, ask):
        stt.side_effect = TranscriptionError("STT HTTP 502")
        await on_voice(msg)
    msg.answer.assert_awaited_once()
    assert "Не смог распознать" in msg.answer.call_args[0][0]
    note.assert_not_awaited()
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_empty_transcript():
    msg = _voice_message()
    with _ready_user(transcript="") as (_stt, note, ask):
        await on_voice(msg)
    assert "пустая" in msg.answer.call_args[0][0]
    note.assert_not_awaited()
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_without_stt_key_reports_unavailable():
    msg = _voice_message()
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value="ory-uuid-123",
        ),
        patch(
            "handlers.voice.get_consent_grant",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("handlers.voice.STT_API_KEY", None),
    ):
        await on_voice(msg)
    assert "недоступны" in msg.answer.call_args[0][0]
    msg.bot.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_fsm_context_does_not_admit_voice():
    assert not await real_fsm_is_clear(None)
    assert await real_fsm_is_clear(
        SimpleNamespace(get_state=AsyncMock(return_value=None))
    )
    assert not await real_fsm_is_clear(
        SimpleNamespace(get_state=AsyncMock(return_value="answering"))
    )


@pytest.mark.asyncio
async def test_consent_does_not_resume_while_a_dialog_is_active(_fsm_clear):
    _fsm_clear.return_value = False
    cb = _consent_callback(_voice_message())
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat", AsyncMock(return_value="account")
        ),
        patch("handlers.voice.set_consent_grant", AsyncMock()) as grant,
        patch("handlers.voice._handle_voice", AsyncMock()) as handle,
    ):
        await on_voice_consent_grant(cb)
    grant.assert_awaited_once()
    handle.assert_not_awaited()
    assert "текущий диалог" in cb.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_consent_does_not_resume_while_bot_waits_for_reply(
    _bot_not_waiting_for_reply,
):
    _bot_not_waiting_for_reply.return_value = True
    cb = _consent_callback(_voice_message())
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat", AsyncMock(return_value="account")
        ),
        patch("handlers.voice.set_consent_grant", AsyncMock()),
        patch("handlers.voice._handle_voice", AsyncMock()) as handle,
    ):
        await on_voice_consent_grant(cb)
    handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_dialog_started_during_recognition_prevents_note_or_question(
    _fsm_clear, _operation_storage
):
    _fsm_clear.side_effect = [True, True, False]
    message = _voice_message()
    with _ready_user() as (stt, note, ask):
        await on_voice(message)
    stt.assert_awaited_once()
    note.assert_not_awaited()
    ask.assert_not_awaited()
    _operation_storage.complete.assert_not_awaited()
    _operation_storage.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_receipt_storage_stops_before_download_and_stt(
    _operation_storage,
):
    _operation_storage.acquire.side_effect = RuntimeError("unavailable")
    message = _voice_message()
    with _ready_user() as (stt, note, _ask):
        await on_voice(message)
    message.bot.download.assert_not_awaited()
    stt.assert_not_awaited()
    note.assert_not_awaited()
    assert "временно недоступны" in message.reply.call_args.args[0]


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_recognition_before_any_write(_operation_storage):
    _operation_storage.renew.return_value = False
    message = _voice_message()

    async def slow_recognition(_audio):
        await asyncio.sleep(1)
        return "мысль"

    with (
        _ready_user() as (stt, note, ask),
        patch("handlers.voice._VOICE_HEARTBEAT_SEC", 0.001),
    ):
        stt.side_effect = slow_recognition
        await on_voice(message)
    note.assert_not_awaited()
    ask.assert_not_awaited()
    _operation_storage.complete.assert_not_awaited()
    assert "Обработка прервалась" in message.reply.call_args.args[0]


# --- metrics -------------------------------------------------------------------


def _metric_lines(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("[voice-metric]")
    ]


@pytest.mark.asyncio
async def test_one_metric_line_per_voice_message_with_outcome_and_timing(caplog):
    msg = _voice_message(duration=7)
    with (
        caplog.at_level(logging.INFO, logger="handlers.voice"),
        _ready_user(transcript="купить молоко"),
    ):
        await on_voice(msg)

    lines = _metric_lines(caplog)
    assert len(lines) == 1
    assert "outcome=note" in lines[0]
    assert "voice_sec=7" in lines[0]
    assert "chars=13" in lines[0]
    assert "купить молоко" not in lines[0]
    assert "stt_ms=-" not in lines[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "setup"),
    [
        ("too_long", {"duration": 120}),
        ("stt_failed", {"stt_error": True}),
        ("empty", {"transcript": ""}),
        ("question", {"transcript": "вопрос как дела"}),
    ],
)
async def test_metric_outcomes(caplog, outcome, setup):
    msg = _voice_message(duration=setup.get("duration", 10))
    with (
        caplog.at_level(logging.INFO, logger="handlers.voice"),
        _ready_user(transcript=setup.get("transcript", "купить молоко")) as (
            stt,
            _note,
            _ask,
        ),
    ):
        if setup.get("stt_error"):
            stt.side_effect = TranscriptionError("boom")
        await on_voice(msg)
    assert f"outcome={outcome}" in _metric_lines(caplog)[0]


# --- consent: the first voice message is not lost ------------------------------


def _consent_callback(voice_message):
    cb = MagicMock()
    cb.from_user.id = 12345
    cb.data = "voice_consent_grant"
    cb.answer = AsyncMock()
    cb.message.reply_to_message = voice_message
    return cb


@pytest.mark.asyncio
async def test_consent_grant_records_consent_and_processes_the_waiting_voice():
    waiting = _voice_message()
    cb = _consent_callback(waiting)
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value="ory-uuid-123",
        ),
        patch("handlers.voice.set_consent_grant", new_callable=AsyncMock) as mock_set,
        patch("handlers.voice.voice_claimable", return_value=True),
        patch("handlers.voice._handle_voice", new_callable=AsyncMock) as handle,
    ):
        await on_voice_consent_grant(cb)

    kwargs = mock_set.call_args.kwargs
    assert kwargs["account_id"] == "ory-uuid-123"
    assert kwargs["scope"] == "voice_processing"
    assert kwargs["granted"] is True
    assert kwargs["consent_version"] == "v1.3"
    assert "Обрабатываю" in cb.answer.call_args[0][0]
    handle.assert_awaited_once_with(waiting, None)


@pytest.mark.asyncio
async def test_consent_grant_without_a_waiting_voice_only_confirms():
    cb = _consent_callback(None)
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value="ory-uuid-123",
        ),
        patch("handlers.voice.set_consent_grant", new_callable=AsyncMock),
        patch("handlers.voice._handle_voice", new_callable=AsyncMock) as handle,
    ):
        await on_voice_consent_grant(cb)
    assert "Отправь голосовое" in cb.answer.call_args[0][0]
    handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_consent_grant_ignores_a_voice_of_another_user():
    other = _voice_message()
    other.from_user.id = 999
    cb = _consent_callback(other)
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value="ory-uuid-123",
        ),
        patch("handlers.voice.set_consent_grant", new_callable=AsyncMock),
        patch("handlers.voice._handle_voice", new_callable=AsyncMock) as handle,
    ):
        await on_voice_consent_grant(cb)
    handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_consent_grant_requires_a_linked_account():
    cb = _consent_callback(None)
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("handlers.voice.set_consent_grant", new_callable=AsyncMock) as mock_set,
    ):
        await on_voice_consent_grant(cb)
    mock_set.assert_not_awaited()
    assert "аккаунт" in cb.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_voice_off_withdraws_the_consent():
    msg = MagicMock()
    msg.from_user.id = 12345
    msg.answer = AsyncMock()
    with (
        patch(
            "handlers.voice.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value="ory-uuid-123",
        ),
        patch("handlers.voice.set_consent_grant", new_callable=AsyncMock) as mock_set,
    ):
        await cmd_voice_off(msg)
    assert mock_set.call_args.kwargs["granted"] is False
    assert mock_set.call_args.kwargs["scope"] == "voice_processing"
    assert "отключены" in msg.answer.call_args[0][0]


# --- undo ----------------------------------------------------------------------


def _undo_callback(data="nu:2:" + "a" * 32 + ":0123456789ab:bbbbbbbbbbbb"):
    cb = MagicMock()
    cb.from_user.id = 12345
    cb.data = data
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    return cb


async def _undo(cb, status=None, error=None):
    remove = AsyncMock(return_value=status, side_effect=error)
    with patch.object(github_notes, "remove_note_block", remove):
        await on_note_undo(cb)
    return remove


@pytest.mark.asyncio
async def test_undo_removes_exactly_that_note_and_replaces_the_message():
    cb = _undo_callback()
    remove = await _undo(cb, RemoveStatus.REMOVED)
    from clients.github_api import NoteRef

    remove.assert_awaited_once_with(
        12345, NoteRef("a" * 32, "0123456789ab", "bbbbbbbbbbbb")
    )
    cb.message.edit_text.assert_awaited_once_with("↩️ Заметка отменена.")


@pytest.mark.asyncio
async def test_undo_of_an_edited_note_leaves_the_file_and_says_so():
    cb = _undo_callback()
    await _undo(cb, RemoveStatus.NOT_FOUND)
    assert "Не нашёл" in cb.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_undo_failure_keeps_the_button():
    cb = _undo_callback()
    await _undo(cb, RemoveStatus.FAILED)
    assert "Не получилось отменить" in cb.answer.call_args[0][0]
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_undo_asks_to_reconnect_github():
    cb = _undo_callback()
    await _undo(cb, error=GitHubAuthUnavailable("gone"))
    assert "/github" in cb.answer.call_args[0][0]
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "nu:",
        "nu:zzzzzzzzzz:6",
        "nu:0123456789",
        "nu:0123456789:0",
        "nu:0123456789:abc",
        "nu:01234:6",
    ],
)
async def test_undo_rejects_malformed_button_data(data):
    cb = _undo_callback(data)
    remove = await _undo(cb, RemoveStatus.REMOVED)
    remove.assert_not_awaited()
    assert "устарела" in cb.answer.call_args[0][0]


# --- question routing ----------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_as_question_routes_prefixed_text():
    from handlers.voice import _ask_as_question

    msg = _voice_message()
    dispatcher = MagicMock(is_sm_active=True, route_message=AsyncMock())
    intern = {"onboarding_completed": True}
    with (
        patch("handlers.get_dispatcher", return_value=dispatcher),
        patch("handlers.voice.get_intern", new_callable=AsyncMock, return_value=intern),
    ):
        routed = await _ask_as_question(msg, "что у меня в плане")

    assert routed is True
    routed_intern, routed_msg = dispatcher.route_message.call_args[0]
    assert routed_intern is intern
    assert routed_msg.text == "?что у меня в плане"
    assert routed_msg.voice is None


@pytest.mark.asyncio
async def test_ask_as_question_skips_not_onboarded():
    from handlers.voice import _ask_as_question

    msg = _voice_message()
    dispatcher = MagicMock(is_sm_active=True, route_message=AsyncMock())
    with (
        patch("handlers.get_dispatcher", return_value=dispatcher),
        patch(
            "handlers.voice.get_intern",
            new_callable=AsyncMock,
            return_value={"onboarding_completed": False},
        ),
    ):
        assert await _ask_as_question(msg, "вопрос") is False
    dispatcher.route_message.assert_not_awaited()
