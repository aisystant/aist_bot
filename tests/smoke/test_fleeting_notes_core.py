"""WP-384: core/fleeting_notes.py - the one way to save a note (typed, voice, Siri).

Covered: the gate and its order, the write outcome mapping, the source label handed to the
notes client, the undo reference, the platform trace (source marker, dedup id, size limit)
and the one metric line per attempt (no note text in it).
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clients.github_api import GitHubNotesClient, NoteTarget
from clients.github_auth import GitHubAuthUnavailable
from core import fleeting_notes as fn
from core.fleeting_notes import NoteSource, NoteStatus

SAVED = {
    "repo": "owner/notes",
    "path": "fleeting-notes.md",
    "branch": "main",
    "sha": "abc",
    "line_start": 7,
    "block_hash": "0123456789ab",
    "block_len": 6,
    "operation_id": "a" * 32,
    "target_hash": NoteTarget("owner/notes", "fleeting-notes.md", "main").fingerprint(),
}


class _Env:
    def __init__(self, has_access, connected, repo, append, trace):
        self.has_access, self.connected, self.repo, self.append, self.trace = (
            has_access,
            connected,
            repo,
            append,
            trace,
        )


def _env(
    has_access=True, connected=True, repo="owner/notes", append=SAVED, append_error=None
):
    append_mock = AsyncMock(
        return_value=append,
        side_effect=GitHubAuthUnavailable("gone") if append_error else None,
    )
    trace = AsyncMock()
    patches = [
        patch(
            "core.access.access_layer.has_access",
            new_callable=AsyncMock,
            return_value=has_access,
        ),
        patch(
            "clients.github_oauth.github_oauth.is_connected",
            new_callable=AsyncMock,
            return_value=connected,
        ),
        patch(
            "clients.github_oauth.github_oauth.get_target_repo",
            new_callable=AsyncMock,
            return_value=repo,
        ),
        patch("clients.github_api.github_notes.append_note", append_mock),
        patch.object(fn, "capture_note_trace", trace),
    ]
    return patches, _Env(has_access, connected, repo, append_mock, trace)


async def _run(coro_factory, **env_kwargs):
    patches, env = _env(**env_kwargs)
    for p in patches:
        p.start()
    try:
        outcome = await coro_factory()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        for p in reversed(patches):
            p.stop()
    return outcome, env


# --- gate ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, None),
        ({"has_access": False}, NoteStatus.NO_ACCESS),
        ({"connected": False}, NoteStatus.NO_GITHUB),
        ({"repo": None}, NoteStatus.NO_REPO),
        ({"has_access": False, "connected": False, "repo": None}, NoteStatus.NO_ACCESS),
        ({"connected": False, "repo": None}, NoteStatus.NO_GITHUB),
    ],
)
async def test_notes_blocker_reports_the_first_missing_condition(kwargs, expected):
    blocker, _ = await _run(lambda: fn.notes_blocker(777), **kwargs)
    assert blocker is expected


@pytest.mark.asyncio
async def test_save_fleeting_note_stops_at_the_gate_without_writing():
    outcome, env = await _run(
        lambda: fn.save_fleeting_note(777, "текст", NoteSource.SIRI), connected=False
    )
    assert outcome == fn.NoteOutcome(NoteStatus.NO_GITHUB)
    env.append.assert_not_awaited()
    env.trace.assert_not_called()


# --- write ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "label"),
    [(NoteSource.TEXT, None), (NoteSource.VOICE, "голос"), (NoteSource.SIRI, "Siri")],
)
async def test_write_note_hands_the_source_label_to_the_notes_client(source, label):
    outcome, env = await _run(lambda: fn.write_note(777, "купить молоко", source))
    assert env.append.await_count == 1
    assert env.append.call_args.args == (777, "купить молоко", label)
    assert callable(env.append.call_args.kwargs["after_attempt"])
    assert outcome.status is NoteStatus.SAVED


@pytest.mark.asyncio
async def test_saved_outcome_has_link_repo_and_undo_reference():
    outcome, _ = await _run(lambda: fn.write_note(777, "текст", NoteSource.VOICE))
    assert outcome.url == "https://github.com/owner/notes/blob/main/fleeting-notes.md"
    assert outcome.repo == "owner/notes"
    assert (
        outcome.undo_ref
        == f"2:{SAVED['operation_id']}:0123456789ab:{SAVED['target_hash']}"
    )


@pytest.mark.asyncio
async def test_saved_outcome_without_fingerprint_has_no_undo_reference():
    legacy = {k: v for k, v in SAVED.items() if k not in ("block_hash", "block_len")}
    outcome, _ = await _run(lambda: fn.write_note(777, "текст"), append=legacy)
    assert outcome.status is NoteStatus.SAVED and outcome.undo_ref is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"append_error": True}, NoteStatus.RECONNECT),
        ({"append": None}, NoteStatus.FAILED),
    ],
)
async def test_failed_writes_are_reported_and_leave_no_trace(kwargs, status):
    outcome, env = await _run(lambda: fn.write_note(777, "текст"), **kwargs)
    assert outcome.status is status
    env.trace.assert_not_called()


@pytest.mark.asyncio
async def test_saved_note_leaves_a_trace_with_the_source_and_the_message_id():
    _outcome, env = await _run(
        lambda: fn.write_note(777, "текст", NoteSource.VOICE, message_id=5)
    )
    env.trace.assert_called_once()
    args, kwargs = env.trace.call_args
    assert args == (777, "текст", 5)
    assert kwargs["source"] is NoteSource.VOICE
    assert kwargs["source_ref"] == SAVED


# --- metric line ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_metric_line_per_attempt_without_the_note_text(caplog):
    with caplog.at_level(logging.INFO, logger="core.fleeting_notes"):
        await _run(lambda: fn.write_note(777, "секретная мысль", NoteSource.SIRI))
        await _run(
            lambda: fn.save_fleeting_note(777, "секретная мысль", NoteSource.VOICE),
            has_access=False,
        )
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("[note-metric]")
    ]
    assert len(lines) == 2
    assert lines[0].startswith("[note-metric] source=siri outcome=saved chars=15 ms=")
    assert lines[1].startswith(
        "[note-metric] source=voice outcome=no_access chars=15 ms="
    )
    assert all("секретная" not in line for line in lines)


# --- trace payload and delivery ------------------------------------------------


def test_payload_carries_source_text_and_reference():
    payload = fn.build_note_payload("текст", NoteSource.SIRI, SAVED)
    assert payload["source"] == "siri"
    assert payload["text"] == "текст"
    assert payload["source_ref"] == {
        "type": "github_blob",
        "repo": "owner/notes",
        "path": "fleeting-notes.md",
        "sha": "abc",
        "branch": "main",
        "line_start": 7,
        "operation_id": "a" * 32,
    }
    assert "block_hash" not in payload["source_ref"]


def test_oversized_payload_keeps_the_reference_and_drops_the_text():
    payload = fn.build_note_payload("я" * 40_000, NoteSource.VOICE, SAVED)
    assert payload["text"] is None
    assert payload["source"] == "voice"
    assert payload["source_ref"]["repo"] == "owner/notes"


async def _capture(message_id, source, source_ref=SAVED, connected=True):
    gateway = AsyncMock()
    gateway.is_connected = lambda _chat: connected
    gateway.capture_trace = AsyncMock()
    with patch("clients.gateway_mcp.gateway_mcp", gateway):
        await fn.capture_note_trace(
            777, "текст", message_id, source=source, source_ref=source_ref
        )
    return gateway


@pytest.mark.asyncio
async def test_trace_dedups_by_telegram_message_id_when_there_is_one():
    legacy = {key: value for key, value in SAVED.items() if key != "operation_id"}
    gateway = await _capture(5, NoteSource.TEXT, source_ref=legacy)
    kwargs = gateway.capture_trace.call_args.kwargs
    assert kwargs["external_id"] == "tg_note_5"
    assert kwargs["sensor_id"] == "bot_note" and kwargs["event_type"] == "note_created"
    assert kwargs["content"]["source"] == "text"


@pytest.mark.asyncio
async def test_trace_of_a_siri_note_dedups_by_operation_identity():
    gateway = await _capture(None, NoteSource.SIRI)
    assert gateway.capture_trace.call_args.kwargs["external_id"] == "note_" + "a" * 32


@pytest.mark.asyncio
async def test_identical_siri_notes_have_distinct_trace_ids_but_retry_keeps_its_id():
    first = await _capture(None, NoteSource.SIRI)
    retry = await _capture(None, NoteSource.SIRI)
    second = await _capture(
        None, NoteSource.SIRI, source_ref={**SAVED, "operation_id": "b" * 32}
    )
    first_id = first.capture_trace.call_args.kwargs["external_id"]
    assert retry.capture_trace.call_args.kwargs["external_id"] == first_id
    assert second.capture_trace.call_args.kwargs["external_id"] != first_id


@pytest.mark.asyncio
async def test_real_queued_retry_uses_common_trace_metric_and_original_source(caplog):
    client = GitHubNotesClient()
    trace = AsyncMock()
    with (
        patch("clients.github_api.github_notes", client),
        patch.object(fn, "notes_blocker", AsyncMock(return_value=None)),
        patch(
            "clients.github_api.github_oauth.get_target_repo",
            AsyncMock(return_value="owner/notes"),
        ),
        patch(
            "clients.github_api.github_oauth.get_notes_path",
            AsyncMock(return_value="fleeting-notes.md"),
        ),
        patch(
            "clients.github_api.github_oauth.get_default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "clients.github_api.resolve_auth_context",
            AsyncMock(return_value=MagicMock()),
        ),
        patch.object(client, "_append_to_file", AsyncMock(side_effect=[None, SAVED])),
        patch.object(fn, "capture_note_trace", trace),
        caplog.at_level(logging.INFO, logger="core.fleeting_notes"),
    ):
        outcome = await fn.write_note(
            777, "исходная мысль", NoteSource.VOICE, message_id=51
        )
        assert outcome.status is NoteStatus.FAILED
        await client.retry_pending()
        await asyncio.sleep(0)
    trace.assert_awaited_once()
    assert trace.call_args.args == (777, "исходная мысль", 51)
    assert trace.call_args.kwargs["source"] is NoteSource.VOICE
    assert trace.call_args.kwargs["source_ref"]["operation_id"]
    metrics = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("[note-metric]")
    ]
    assert len(metrics) == 2
    assert "outcome=failed" in metrics[0] and "outcome=saved" in metrics[1]
    assert all("исходная мысль" not in metric for metric in metrics)


@pytest.mark.asyncio
async def test_trace_is_skipped_for_a_user_not_connected_to_the_platform():
    gateway = await _capture(5, NoteSource.TEXT, connected=False)
    gateway.capture_trace.assert_not_awaited()


@pytest.mark.asyncio
async def test_trace_failure_never_breaks_the_note():
    gateway = AsyncMock()
    gateway.is_connected = lambda _chat: True
    gateway.capture_trace = AsyncMock(side_effect=RuntimeError("gateway down"))
    with patch("clients.gateway_mcp.gateway_mcp", gateway):
        await fn.capture_note_trace(
            777, "текст", 5, source=NoteSource.TEXT, source_ref=SAVED
        )
