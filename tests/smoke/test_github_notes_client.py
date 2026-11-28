"""WP-384: the fleeting-notes client (clients/github_api.py).

- the date line carries the source («· голос», «· Siri») only for dictated notes;
- the block of a saved note can be found again by its fingerprint and removed
  without touching anything else (undo), also after newer notes were added above it;
- an edited or already removed note is left alone;
- one write attempt never queues a retry by itself, so a failed retry does not
  multiply the queued notes (it used to double them on every cycle).
"""

import base64
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.github_api import (
    MOSCOW_TZ,
    GitHubNotesClient,
    NoteRef,
    NoteTarget,
    RemoveStatus,
    find_note_operation,
    note_block_hash,
    operation_marker,
    parse_note_ref,
)
from clients.github_auth import GitHubAuthUnavailable

NOW = datetime(2026, 11, 28, 10, 24, tzinfo=MOSCOW_TZ)

HEADER = [
    "---",
    "type: inbox",
    "---",
    "",
    "# Fleeting Notes",
    "",
    "> Быстрые заметки",
    "",
]
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


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("голос", "<sub>28 ноя, 10:24 · голос</sub>"),
        ("Siri", "<sub>28 ноя, 10:24 · Siri</sub>"),
    ],
)
def test_dictated_note_date_line_names_the_source(label, expected):
    assert _block("Купить молоко", label)[3] == expected


def test_block_shape_is_unchanged_for_a_note_with_body():
    block = _block("Идея. Связать экзоскелет мышления с РП-392")
    assert block[:2] == ["---", ""]
    assert block[2] == "**Идея.**"
    assert block[4:] == ["", "Связать экзоскелет мышления с РП-392", ""]


# --- find / hash ---------------------------------------------------------------


def _note(text, label=None, operation_id="a" * 32):
    return [
        operation_marker(operation_id, "start"),
        *_block(text, label),
        operation_marker(operation_id, "end"),
        "",
    ]


def _ref(block):
    target = NoteTarget("owner/notes", "fleeting-notes.md", "main")
    return NoteRef(
        block[0].split(":")[2], note_block_hash(block[:-1]), target.fingerprint()
    )


def test_hash_depends_on_every_physical_line():
    block = _note("Идея.\nПервая строка\nВторая строка")
    changed = block.copy()
    changed[-3] += "changed"
    assert len(note_block_hash(block)) == 12
    assert note_block_hash(block) != note_block_hash(changed)
    assert all("\n" not in line for line in block)


def test_find_operation_after_newer_notes_and_at_end():
    mine = _note("Купить молоко")
    newer = _note("Позвонить", operation_id="b" * 32)
    lines = _file(newer, mine)
    start, end = find_note_operation(lines, "a" * 32)
    assert lines[start:end] == mine[:-1]
    assert start == len(HEADER) + len(newer)


@pytest.mark.parametrize(
    "kind", ["missing_end", "missing_start", "duplicate", "reversed"]
)
def test_partial_or_ambiguous_operation_is_not_selected(kind):
    block = _note("Купить молоко")
    if kind == "missing_end":
        lines = block[:-2]
    elif kind == "missing_start":
        lines = block[1:]
    elif kind == "duplicate":
        lines = block + block
    else:
        lines = [block[-2], block[0]]
    assert find_note_operation(lines, "a" * 32) is None


def test_reference_is_versioned_and_fits_telegram_limit():
    encoded = _ref(_note("Купить молоко")).encode()
    assert parse_note_ref(encoded) == _ref(_note("Купить молоко"))
    assert len(("nu:" + encoded).encode("utf-8")) <= 64
    assert parse_note_ref("0123456789:6") is None


# --- remove --------------------------------------------------------------------


def _client_with_file(content, put_results=("ok",)):
    client = GitHubNotesClient()
    fetch = AsyncMock(
        side_effect=[(content, f"sha{i}") for i in range(len(put_results))]
    )
    put = AsyncMock(side_effect=list(put_results))
    return client, fetch, put


async def _remove(client, fetch, put, block, user=777):
    auth = MagicMock()
    with (
        patch(
            "clients.github_api.github_oauth.get_target_repo",
            new_callable=AsyncMock,
            return_value="owner/notes",
        ),
        patch(
            "clients.github_api.github_oauth.get_notes_path",
            new_callable=AsyncMock,
            return_value="fleeting-notes.md",
        ),
        patch(
            "clients.github_api.github_oauth.get_default_branch",
            new_callable=AsyncMock,
            return_value="main",
        ),
        patch(
            "clients.github_api.resolve_auth_context",
            new_callable=AsyncMock,
            return_value=auth,
        ),
        patch.object(client, "_fetch_file", fetch),
        patch.object(client, "_put_file", put),
    ):
        return await client.remove_note_block(user, _ref(block))


@pytest.mark.asyncio
async def test_remove_writes_the_file_back_without_exactly_that_block():
    mine = _note("Купить молоко", "голос")
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
    mine = _note("Купить молоко")
    client, fetch, put = _client_with_file("\n".join(_file(OLD_NOTE)))
    assert await _remove(client, fetch, put, mine) is RemoveStatus.NOT_FOUND
    put.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_retries_after_a_sha_conflict_and_succeeds():
    mine = _note("Купить молоко")
    content = "\n".join(_file(mine, OLD_NOTE))
    client, fetch, put = _client_with_file(content, put_results=("conflict", "ok"))
    assert await _remove(client, fetch, put, mine) is RemoveStatus.REMOVED
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_remove_gives_up_after_repeated_conflicts():
    mine = _note("Купить молоко")
    content = "\n".join(_file(mine, OLD_NOTE))
    client, fetch, put = _client_with_file(content, put_results=("conflict",) * 3)
    assert await _remove(client, fetch, put, mine) is RemoveStatus.FAILED


@pytest.mark.asyncio
async def test_remove_reports_failure_when_the_file_cannot_be_read_or_written():
    mine = _note("Купить молоко")
    content = "\n".join(_file(mine))
    unreadable = GitHubNotesClient()
    assert (
        await _remove(unreadable, AsyncMock(return_value=None), AsyncMock(), mine)
        is RemoveStatus.FAILED
    )
    client, fetch, put = _client_with_file(content, put_results=("error",))
    assert await _remove(client, fetch, put, mine) is RemoveStatus.FAILED


@pytest.mark.asyncio
async def test_remove_without_a_notes_repo_fails():
    client = GitHubNotesClient()
    with patch(
        "clients.github_api.github_oauth.get_target_repo",
        new_callable=AsyncMock,
        return_value=None,
    ):
        assert (
            await client.remove_note_block(777, _ref(_note("x"))) is RemoveStatus.FAILED
        )


@pytest.mark.asyncio
async def test_remove_propagates_missing_authorization():
    client = GitHubNotesClient()
    with (
        patch(
            "clients.github_api.github_oauth.get_target_repo",
            new_callable=AsyncMock,
            return_value="owner/notes",
        ),
        patch(
            "clients.github_api.github_oauth.get_notes_path",
            new_callable=AsyncMock,
            return_value="fleeting-notes.md",
        ),
        patch(
            "clients.github_api.github_oauth.get_default_branch",
            new_callable=AsyncMock,
            return_value="main",
        ),
        patch(
            "clients.github_api.resolve_auth_context",
            new_callable=AsyncMock,
            side_effect=GitHubAuthUnavailable("gone"),
        ),
        pytest.raises(GitHubAuthUnavailable),
    ):
        await client.remove_note_block(777, _ref(_note("x")))


# --- append / retry ------------------------------------------------------------


def _patch_env():
    return (
        patch(
            "clients.github_api.github_oauth.get_target_repo",
            new_callable=AsyncMock,
            return_value="owner/notes",
        ),
        patch(
            "clients.github_api.github_oauth.get_notes_path",
            new_callable=AsyncMock,
            return_value="fleeting-notes.md",
        ),
        patch(
            "clients.github_api.github_oauth.get_default_branch",
            new_callable=AsyncMock,
            return_value="main",
        ),
        patch(
            "clients.github_api.resolve_auth_context",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
    )


WRITTEN = {
    "repo": "owner/notes",
    "path": "fleeting-notes.md",
    "branch": "main",
    "sha": "s",
    "line_start": 9,
}


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
    assert result["block_hash"] == note_block_hash(block[:-1])
    assert result["block_len"] == len(block)
    assert block[4].endswith("· голос</sub>")
    assert client._pending == []


@pytest.mark.asyncio
async def test_failed_append_retries_with_same_identity_text_time_source_and_destination():
    client = GitHubNotesClient()
    result, first = await _append(client, None, "Siri")
    assert result is None and len(client._pending) == 1
    second = await _retry(client, WRITTEN)
    assert second.await_count == 1
    for field in ("repo", "path", "branch", "note_lines", "operation_id"):
        assert second.call_args.kwargs[field] == first.call_args.kwargs[field]
    assert "Siri</sub>" in "\n".join(second.call_args.kwargs["note_lines"])
    assert client._pending == []


@pytest.mark.asyncio
async def test_retries_are_bounded_and_do_not_multiply():
    client = GitHubNotesClient()
    await _append(client, None, "голос")
    for _ in range(client._MAX_RETRIES - 1):
        write = await _retry(client, None)
        assert write.await_count == 1 and len(client._pending) == 1
    last = await _retry(client, None)
    assert last.await_count == 1 and client._pending == []


@pytest.mark.asyncio
async def test_successful_retry_calls_the_original_completion_once():
    client = GitHubNotesClient()
    completed = AsyncMock()
    a, b, c, d = _patch_env()
    with (
        a,
        b,
        c,
        d,
        patch.object(client, "_append_to_file", AsyncMock(return_value=None)),
    ):
        await client.append_note(777, "Купить молоко", "голос", after_attempt=completed)
    await _retry(client, WRITTEN)
    completed.assert_awaited_once()
    assert completed.call_args.args[0]["operation_id"]
    assert completed.call_args.args[0]["repo"] == "owner/notes"


@pytest.mark.asyncio
async def test_retry_keeps_original_destination_when_settings_change():
    client = GitHubNotesClient()
    await _append(client, None)
    with (
        patch(
            "clients.github_api.github_oauth.get_target_repo",
            AsyncMock(return_value="other/repo"),
        ) as target,
        patch(
            "clients.github_api.resolve_auth_context",
            AsyncMock(return_value=MagicMock()),
        ),
        patch.object(
            client, "_append_to_file", AsyncMock(return_value=WRITTEN)
        ) as write,
    ):
        await client.retry_pending()
    target.assert_not_awaited()
    assert write.call_args.kwargs["repo"] == "owner/notes"
    assert client._pending == []


@pytest.mark.asyncio
async def test_retry_drops_missing_authorization():
    client = GitHubNotesClient()
    await _append(client, None)
    with patch(
        "clients.github_api.resolve_auth_context",
        AsyncMock(side_effect=GitHubAuthUnavailable("gone")),
    ):
        await client.retry_pending()
    assert client._pending == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_old_button_never_removes_a_different_identical_note(changed):
    mine = _note("Купить молоко", operation_id="a" * 32)
    identical = _note("Купить молоко", operation_id="b" * 32)
    original = mine.copy()
    if changed:
        mine[3] = "**Изменено**"
        content = "\n".join(_file(identical, mine))
    else:
        content = "\n".join(_file(identical))
    client, fetch, put = _client_with_file(content)
    assert await _remove(client, fetch, put, original) is RemoveStatus.NOT_FOUND
    put.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiline_note_undo_removes_every_physical_line():
    mine = _note("Идея.\nПервая строка\nВторая строка\n---\nЕщё строка", "голос")
    client, fetch, put = _client_with_file("\n".join(_file(mine, OLD_NOTE)))
    assert await _remove(client, fetch, put, mine) is RemoveStatus.REMOVED
    assert put.call_args.args[4] == "\n".join(_file(OLD_NOTE))


@pytest.mark.asyncio
async def test_reference_cannot_delete_in_a_changed_target():
    block = _note("Купить молоко")
    client = GitHubNotesClient()
    target = NoteTarget("other/repo", "fleeting-notes.md", "main")
    with (
        patch.object(client, "resolve_target", AsyncMock(return_value=target)),
        patch.object(client, "_fetch_file", AsyncMock()) as fetch,
    ):
        assert (
            await client.remove_note_block(777, _ref(block)) is RemoveStatus.NOT_FOUND
        )
    fetch.assert_not_awaited()


class _Response:
    def __init__(self, payload, *, lose_response=False):
        self.status = 200
        self.payload = payload
        self.lose_response = lose_response

    async def __aenter__(self):
        if self.lose_response:
            raise TimeoutError("response lost after the server committed")
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


class _MemoryContentsAPI:
    """A deterministic Contents API with an optional lost PUT acknowledgement."""

    def __init__(self, lose_response=False):
        self.content = "# Fleeting Notes\n\n"
        self.puts = 0
        self.lose_response = lose_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        return _Response(
            {
                "sha": f"sha-{self.puts}",
                "content": base64.b64encode(self.content.encode()).decode(),
            }
        )

    def put(self, *args, json, **kwargs):
        self.puts += 1
        self.content = base64.b64decode(json["content"]).decode()
        lost, self.lose_response = self.lose_response, False
        return _Response({"content": {"sha": f"sha-{self.puts}"}}, lose_response=lost)


@pytest.mark.asyncio
async def test_lost_put_response_does_not_append_the_operation_twice():
    client = GitHubNotesClient()
    server = _MemoryContentsAPI(lose_response=True)
    a, b, c, d = _patch_env()
    with a, b, c, d, patch("aiohttp.ClientSession", return_value=server):
        result = await client.append_note(777, "Купить молоко", "голос")
    assert server.puts == 1
    assert server.content.count(operation_marker(result["operation_id"], "start")) == 1
    assert result["content_matches"] is True
    assert client._pending == []


@pytest.mark.asyncio
async def test_two_identical_siri_notes_in_one_minute_have_distinct_identities():
    client = GitHubNotesClient()
    server = _MemoryContentsAPI()
    a, b, c, d = _patch_env()
    with (
        a,
        b,
        c,
        d,
        patch("aiohttp.ClientSession", return_value=server),
        patch("clients.github_api.datetime") as clock,
    ):
        clock.now.return_value = NOW
        first = await client.append_note(777, "Купить молоко", "Siri")
        second = await client.append_note(777, "Купить молоко", "Siri")
    assert server.puts == 2 and first["operation_id"] != second["operation_id"]
    assert server.content.count("**Купить молоко**") == 2


@pytest.mark.asyncio
async def test_recovered_operation_with_changed_content_never_claims_the_retry_text():
    client = GitHubNotesClient()
    server = _MemoryContentsAPI()
    a, b, c, d = _patch_env()
    with a, b, c, d, patch("aiohttp.ClientSession", return_value=server):
        first = await client.append_note(777, "Купить молоко", "голос")
        retry = await client.append_note(
            777, "Другая расшифровка", "голос", operation_id=first["operation_id"]
        )
    assert server.puts == 1 and retry["content_matches"] is False
    assert "Другая расшифровка" not in server.content


@pytest.mark.asyncio
async def test_recovered_multiline_operation_keeps_original_time_and_undo_reference():
    from core.fleeting_notes import _saved_outcome

    client = GitHubNotesClient()
    server = _MemoryContentsAPI()
    a, b, c, d = _patch_env()
    with a, b, c, d, patch("aiohttp.ClientSession", return_value=server):
        first = await client.append_note(
            777, "Первая строка\nВторая строка", "голос", created_at=NOW
        )
        original_content = server.content
        with patch("clients.github_api.datetime") as clock:
            clock.now.return_value = NOW.replace(minute=NOW.minute + 1)
            retry = await client.append_note(
                777,
                "Первая строка\nВторая строка",
                "голос",
                operation_id=first["operation_id"],
                created_at=NOW,
            )
    assert server.puts == 1 and server.content == original_content
    assert retry["content_matches"] is True
    assert _saved_outcome(retry).undo_ref == _saved_outcome(first).undo_ref
    assert _saved_outcome(retry).undo_ref is not None


@pytest.mark.asyncio
async def test_original_block_fingerprint_recovers_forward_merge_but_refuses_edited_block():
    from core.fleeting_notes import _saved_outcome

    client = GitHubNotesClient()
    server = _MemoryContentsAPI()
    bound = []

    async def bind(digest):
        if not bound:
            bound.append(digest)
        return bound[0]

    a, b, c, d = _patch_env()
    with a, b, c, d, patch("aiohttp.ClientSession", return_value=server):
        first = await client.append_note(
            777,
            "Мой комментарий\n\nПересланный текст",
            "голос",
            created_at=NOW,
            bind_block_hash=bind,
        )
        retry = await client.append_note(
            777,
            "Мой комментарий",
            "голос",
            operation_id=first["operation_id"],
            created_at=NOW,
            bind_block_hash=bind,
        )
        assert server.puts == 1
        assert retry["content_matches"] is True and retry["input_matches"] is False
        assert _saved_outcome(retry).undo_ref == _saved_outcome(first).undo_ref
        server.content = server.content.replace("Пересланный текст", "Изменённый текст")
        edited = await client.append_note(
            777,
            "Мой комментарий",
            "голос",
            operation_id=first["operation_id"],
            created_at=NOW,
            bind_block_hash=bind,
        )
    assert server.puts == 1 and edited["content_matches"] is False
    assert _saved_outcome(edited).undo_ref is None


@pytest.mark.asyncio
async def test_changed_retry_candidate_cannot_create_a_new_block():
    client = GitHubNotesClient()
    server = _MemoryContentsAPI()
    binder = AsyncMock(return_value="f" * 12)
    a, b, c, d = _patch_env()
    with a, b, c, d, patch("aiohttp.ClientSession", return_value=server):
        result = await client.append_note(
            777, "Новая расшифровка", "голос", created_at=NOW, bind_block_hash=binder
        )
    assert result is None and server.puts == 0
