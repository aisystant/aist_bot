"""WP-384: the fleeting-notes client (clients/github_api.py).

- the date line carries the source («· голос», «· Siri») only for dictated notes;
- the block of a saved note can be found again by its fingerprint and removed
  without touching anything else (undo), also after newer notes were added above it;
- an edited or already removed note is left alone;
- one write attempt never queues a retry by itself, so a failed retry does not
  multiply the queued notes (it used to double them on every cycle).
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.github_api import (
    GitHubNotesClient,
    MOSCOW_TZ,
    RemoveStatus,
    find_note_block,
    note_block_hash,
)
from clients.github_auth import GitHubAuthUnavailable

NOW = datetime(2026, 11, 28, 10, 24, tzinfo=MOSCOW_TZ)

HEADER = ["---", "type: inbox", "---", "", "# Fleeting Notes", "", "> Быстрые заметки", ""]
OLD_NOTE = ["---", "", "**Старая мысль**", "<sub>27 ноя, 09:00</sub>", ""]


def _block(text, label=None):
    return GitHubNotesClient._format_note_lines(text, NOW, label)


def _file(*blocks):
    lines = list(HEADER)
    for block in blocks:
        lines.extend(block)
    return lines


# --- format --------------------------------------------------------------------

def test_typed_note_date_line_has_no_source():
    assert _block("Купить молоко")[3] == "<sub>28 ноя, 10:24</sub>"


@pytest.mark.parametrize(("label", "expected"), [("голос", "<sub>28 ноя, 10:24 · голос</sub>"), ("Siri", "<sub>28 ноя, 10:24 · Siri</sub>")])
def test_dictated_note_date_line_names_the_source(label, expected):
    assert _block("Купить молоко", label)[3] == expected


def test_block_shape_is_unchanged_for_a_note_with_body():
    block = _block("Идея. Связать экзоскелет мышления с РП-392")
    assert block[:2] == ["---", ""]
    assert block[2] == "**Идея.**"
    assert block[4:] == ["", "Связать экзоскелет мышления с РП-392", ""]


# --- find / hash ---------------------------------------------------------------

def test_hash_is_short_and_depends_on_every_line():
    block = _block("Купить молоко")
    changed = list(block)
    changed[3] = "<sub>28 ноя, 10:25</sub>"
    assert len(note_block_hash(block)) == 10
    assert note_block_hash(block) != note_block_hash(changed)


def test_find_note_block_returns_its_start_even_after_newer_notes_were_added_above():
    mine = _block("Купить молоко", "голос")
    newer = _block("Позвонить Андрею")
    lines = _file(newer, mine, OLD_NOTE)
    start = find_note_block(lines, note_block_hash(mine), len(mine))
    assert lines[start:start + len(mine)] == mine
    assert start == len(HEADER) + len(newer)


def test_find_note_block_when_it_is_the_last_block_of_the_file():
    mine = _block("Купить молоко")
    lines = _file(OLD_NOTE, mine)
    assert find_note_block(lines, note_block_hash(mine), len(mine)) == len(HEADER) + len(OLD_NOTE)


def test_find_note_block_ignores_an_edited_note():
    mine = _block("Купить молоко")
    lines = _file(mine, OLD_NOTE)
    lines[len(HEADER) + 2] = "**Купить кефир**"
    assert find_note_block(lines, note_block_hash(mine), len(mine)) is None


def test_find_note_block_does_not_match_a_window_that_runs_past_the_file_end():
    mine = _block("Купить молоко")
    assert find_note_block(["---", ""], note_block_hash(mine), len(mine)) is None


# --- remove --------------------------------------------------------------------

def _client_with_file(content, put_results=("ok",)):
    client = GitHubNotesClient()
    fetch = AsyncMock(side_effect=[(content, f"sha{i}") for i in range(len(put_results))])
    put = AsyncMock(side_effect=list(put_results))
    return client, fetch, put


async def _remove(client, fetch, put, block, user=777):
    auth = MagicMock()
    with patch("clients.github_api.github_oauth.get_target_repo", new_callable=AsyncMock, return_value="owner/notes"), \
         patch("clients.github_api.github_oauth.get_notes_path", new_callable=AsyncMock, return_value="fleeting-notes.md"), \
         patch("clients.github_api.github_oauth.get_default_branch", new_callable=AsyncMock, return_value="main"), \
         patch("clients.github_api.resolve_auth_context", new_callable=AsyncMock, return_value=auth), \
         patch.object(client, "_fetch_file", fetch), \
         patch.object(client, "_put_file", put):
        return await client.remove_note_block(user, note_block_hash(block), len(block))


@pytest.mark.asyncio
async def test_remove_writes_the_file_back_without_exactly_that_block():
    mine = _block("Купить молоко", "голос")
    newer = _block("Позвонить Андрею")
    content = "\n".join(_file(newer, mine, OLD_NOTE))
    client, fetch, put = _client_with_file(content)

    status = await _remove(client, fetch, put, mine)

    assert status is RemoveStatus.REMOVED
    written = put.call_args[0][4]
    assert written == "\n".join(_file(newer, OLD_NOTE))
    assert put.call_args[0][5] == "sha0"


@pytest.mark.asyncio
async def test_remove_of_a_missing_note_writes_nothing():
    mine = _block("Купить молоко")
    client, fetch, put = _client_with_file("\n".join(_file(OLD_NOTE)))
    assert await _remove(client, fetch, put, mine) is RemoveStatus.NOT_FOUND
    put.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_retries_after_a_sha_conflict_and_succeeds():
    mine = _block("Купить молоко")
    content = "\n".join(_file(mine, OLD_NOTE))
    client, fetch, put = _client_with_file(content, put_results=("conflict", "ok"))
    assert await _remove(client, fetch, put, mine) is RemoveStatus.REMOVED
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_remove_gives_up_after_repeated_conflicts():
    mine = _block("Купить молоко")
    content = "\n".join(_file(mine, OLD_NOTE))
    client, fetch, put = _client_with_file(content, put_results=("conflict",) * 3)
    assert await _remove(client, fetch, put, mine) is RemoveStatus.FAILED


@pytest.mark.asyncio
async def test_remove_reports_failure_when_the_file_cannot_be_read_or_written():
    mine = _block("Купить молоко")
    content = "\n".join(_file(mine))
    unreadable = GitHubNotesClient()
    assert await _remove(unreadable, AsyncMock(return_value=None), AsyncMock(), mine) is RemoveStatus.FAILED
    client, fetch, put = _client_with_file(content, put_results=("error",))
    assert await _remove(client, fetch, put, mine) is RemoveStatus.FAILED


@pytest.mark.asyncio
async def test_remove_without_a_notes_repo_fails():
    client = GitHubNotesClient()
    with patch("clients.github_api.github_oauth.get_target_repo", new_callable=AsyncMock, return_value=None):
        assert await client.remove_note_block(777, "0123456789", 5) is RemoveStatus.FAILED


@pytest.mark.asyncio
async def test_remove_propagates_missing_authorization():
    client = GitHubNotesClient()
    with patch("clients.github_api.github_oauth.get_target_repo", new_callable=AsyncMock, return_value="owner/notes"), \
         patch("clients.github_api.github_oauth.get_notes_path", new_callable=AsyncMock, return_value="n.md"), \
         patch("clients.github_api.resolve_auth_context", new_callable=AsyncMock, side_effect=GitHubAuthUnavailable("gone")):
        with pytest.raises(GitHubAuthUnavailable):
            await client.remove_note_block(777, "0123456789", 5)


# --- append / retry ------------------------------------------------------------

def _patch_env():
    return (
        patch("clients.github_api.github_oauth.get_target_repo", new_callable=AsyncMock, return_value="owner/notes"),
        patch("clients.github_api.github_oauth.get_notes_path", new_callable=AsyncMock, return_value="fleeting-notes.md"),
        patch("clients.github_api.github_oauth.get_default_branch", new_callable=AsyncMock, return_value="main"),
        patch("clients.github_api.resolve_auth_context", new_callable=AsyncMock, return_value=MagicMock()),
    )


WRITTEN = {"repo": "owner/notes", "path": "fleeting-notes.md", "branch": "main", "sha": "s", "line_start": 9}


async def _append(client, result, label=None):
    """append_note with the file write mocked; returns (result, the mocked file write)."""
    write = AsyncMock(return_value=result)
    a, b, c, d = _patch_env()
    with a, b, c, d, patch.object(client, "_append_to_file", write):
        returned = await client.append_note(777, "Купить молоко", label)
    return returned, write


async def _retry(client, result):
    """retry_pending with the file write mocked; returns the mocked file write."""
    write = AsyncMock(return_value=result)
    a, b, c, d = _patch_env()
    with a, b, c, d, patch.object(client, "_append_to_file", write):
        await client.retry_pending()
    return write


@pytest.mark.asyncio
async def test_append_note_returns_block_fingerprint_and_length_for_undo():
    client = GitHubNotesClient()
    result, write = await _append(client, WRITTEN, "голос")
    block = write.call_args.kwargs["note_lines"]
    assert result["block_hash"] == note_block_hash(block)
    assert result["block_len"] == len(block)
    assert block[3].endswith("· голос</sub>")
    assert client._pending == []


@pytest.mark.asyncio
async def test_failed_append_is_queued_once_with_its_source_label():
    client = GitHubNotesClient()
    result, _write = await _append(client, None, "Siri")
    assert result is None
    assert client._pending == [(777, "Купить молоко", 1, "Siri")]


@pytest.mark.asyncio
async def test_failed_retry_requeues_one_entry_not_two():
    client = GitHubNotesClient()
    client._pending = [(777, "Купить молоко", 1, "голос")]
    await _retry(client, None)
    assert client._pending == [(777, "Купить молоко", 2, "голос")]


@pytest.mark.asyncio
async def test_retry_drops_the_note_after_the_last_attempt():
    client = GitHubNotesClient()
    client._pending = [(777, "Купить молоко", client._MAX_RETRIES, None)]
    await _retry(client, None)
    assert client._pending == []


@pytest.mark.asyncio
async def test_successful_retry_writes_the_note_once_and_empties_the_queue():
    client = GitHubNotesClient()
    client._pending = [(777, "Купить молоко", 2, "голос")]
    write = await _retry(client, WRITTEN)
    assert client._pending == []
    assert write.await_count == 1
    assert write.call_args.kwargs["note_lines"][3].endswith("· голос</sub>")


@pytest.mark.asyncio
async def test_retry_drops_a_note_when_the_notes_repo_is_gone():
    client = GitHubNotesClient()
    client._pending = [(777, "Купить молоко", 1, None)]
    with patch("clients.github_api.github_oauth.get_target_repo", new_callable=AsyncMock, return_value=None):
        await client.retry_pending()
    assert client._pending == []
