"""Characterization tests for the typed ".text" fleeting note (handlers/github.py).

They pin the behaviour users had before the typed, voice and Siri paths were merged
into one save function (core/fleeting_notes.py, WP-384): gate order (access -> GitHub ->
notes repo), reply / pending-forward merging, the answers, and the platform trace after a
successful write. The tests were written against the old code first and then kept green.
"""

import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import handlers.github as gh
from clients.github_auth import GitHubAuthUnavailable

URL = "https://github.com/owner/notes/blob/main/fleeting-notes.md"
SAVED = {"repo": "owner/notes", "path": "fleeting-notes.md", "branch": "main", "sha": "abc", "line_start": 7}


def _message(text, reply_to=None, message_id=5):
    message = MagicMock()
    message.chat.id = 777
    message.message_id = message_id
    message.text = text
    message.reply_to_message = reply_to
    message.answer = AsyncMock()
    return message


def _replied(text, forwarded=False):
    replied = MagicMock()
    replied.text = text
    replied.caption = None
    replied.forward_origin = MagicMock() if forwarded else None
    replied.forward_from = None
    replied.forward_sender_name = None
    if forwarded:
        replied.forward_origin.sender_user.full_name = "Андрей"
    return replied


@pytest.fixture(autouse=True)
def _clean_pending():
    gh._pending_forwards.clear()
    gh._pending_forward_messages.clear()
    yield
    gh._pending_forwards.clear()
    gh._pending_forward_messages.clear()


@contextmanager
def _env(has_access=True, connected=True, repo="owner/notes", append=SAVED, append_error=None):
    append_mock = AsyncMock(return_value=append, side_effect=GitHubAuthUnavailable("gone") if append_error else None)
    trace = AsyncMock()
    with patch("core.access.access_layer.has_access", new_callable=AsyncMock, return_value=has_access), \
         patch("core.access.access_layer.get_paywall", new_callable=AsyncMock, return_value=("PAYWALL", "KEYBOARD")), \
         patch("clients.github_oauth.github_oauth.is_connected", new_callable=AsyncMock, return_value=connected), \
         patch("clients.github_oauth.github_oauth.get_target_repo", new_callable=AsyncMock, return_value=repo), \
         patch("clients.github_api.github_notes.append_note", append_mock), \
         patch.object(gh, "get_intern", new_callable=AsyncMock, return_value={"language": "ru"}), \
         patch("core.fleeting_notes.capture_note_trace", trace):
        yield append_mock, trace


async def _drain_tasks():
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _first_arg(message):
    return message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_dot_text_is_saved_and_answered_with_link_and_trace():
    message = _message(".купить молоко")
    with _env() as (append, trace):
        await gh.handle_fleeting_note(message)
        await _drain_tasks()

    assert append.call_args[0][:2] == (777, "купить молоко")
    assert _first_arg(message) == f"Записано → {URL}"
    trace.assert_called_once()
    assert trace.call_args[0][:3] == (777, "купить молоко", 5)
    assert trace.call_args.kwargs["source_ref"] == SAVED


@pytest.mark.asyncio
async def test_without_access_shows_paywall_and_writes_nothing():
    message = _message(".заметка")
    with _env(has_access=False) as (append, trace):
        await gh.handle_fleeting_note(message)
    message.answer.assert_awaited_once()
    assert message.answer.call_args[0][0] == "PAYWALL"
    assert message.answer.call_args.kwargs["reply_markup"] == "KEYBOARD"
    append.assert_not_awaited()
    trace.assert_not_called()


@pytest.mark.asyncio
async def test_without_github_asks_to_connect():
    message = _message(".заметка")
    with _env(connected=False) as (append, _trace):
        await gh.handle_fleeting_note(message)
    assert "подключите GitHub" in _first_arg(message)
    append.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_notes_repo_asks_to_choose_one():
    message = _message(".заметка")
    with _env(repo=None) as (append, _trace):
        await gh.handle_fleeting_note(message)
    assert "Репозиторий не выбран" in _first_arg(message)
    append.assert_not_awaited()


@pytest.mark.asyncio
async def test_bare_dot_waits_for_a_forward_and_writes_nothing():
    message = _message(".")
    with _env() as (append, _trace):
        await gh.handle_fleeting_note(message)
    message.answer.assert_not_awaited()
    append.assert_not_awaited()
    assert 777 in gh._pending_forwards


@pytest.mark.asyncio
async def test_reply_to_message_merges_comment_and_replied_text():
    message = _message(".важно", reply_to=_replied("текст сообщения"))
    with _env() as (append, _trace):
        await gh.handle_fleeting_note(message)
    assert append.call_args[0][1] == "Мой комментарий: важно\n\nтекст сообщения"


@pytest.mark.asyncio
async def test_forward_then_dot_merges_forwarded_text():
    forwarded = _replied("пересланный текст", forwarded=True)
    gh._pending_forward_messages[777] = (forwarded, time.time())
    message = _message(".мой комментарий")
    with _env() as (append, _trace):
        await gh.handle_fleeting_note(message)
    text = append.call_args[0][1]
    assert text.startswith("Мой комментарий: мой комментарий\n\n")
    assert "пересланный текст" in text


@pytest.mark.asyncio
async def test_expired_forward_is_not_merged():
    forwarded = _replied("старый текст", forwarded=True)
    gh._pending_forward_messages[777] = (forwarded, time.time() - gh._FORWARD_TTL_SECONDS - 5)
    message = _message(".заметка")
    with _env() as (append, _trace):
        await gh.handle_fleeting_note(message)
    assert append.call_args[0][1] == "заметка"


@pytest.mark.asyncio
async def test_auth_unavailable_asks_to_reconnect():
    message = _message(".заметка")
    with _env(append_error=True) as (_append, trace):
        await gh.handle_fleeting_note(message)
    assert "переподключить GitHub" in _first_arg(message)
    trace.assert_not_called()


@pytest.mark.asyncio
async def test_failed_write_reports_error_without_trace():
    message = _message(".заметка")
    with _env(append=None) as (_append, trace):
        await gh.handle_fleeting_note(message)
    assert "Не удалось записать заметку" in _first_arg(message)
    trace.assert_not_called()


@pytest.mark.asyncio
async def test_dot_then_forward_saves_the_forward_with_the_comment():
    gh._pending_forwards[777] = ("мысль", time.time())
    forwarded = _replied("пересланный текст", forwarded=True)
    forwarded.chat.id = 777
    forwarded.message_id = 9
    forwarded.answer = AsyncMock()
    with _env() as (append, trace):
        await gh.handle_forwarded_message(forwarded)
        await _drain_tasks()

    text = append.call_args[0][1]
    assert text.startswith("Мой комментарий: мысль\n\n")
    assert forwarded.answer.call_args[0][0] == f"Записано → {URL}"
    trace.assert_called_once()


@pytest.mark.asyncio
async def test_forward_without_pending_dot_is_remembered_for_a_later_dot():
    forwarded = _replied("пересланный текст", forwarded=True)
    forwarded.chat.id = 777
    with _env() as (append, _trace):
        await gh.handle_forwarded_message(forwarded)
    append.assert_not_awaited()
    assert gh._pending_forward_messages[777][0] is forwarded
