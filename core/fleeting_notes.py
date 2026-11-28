"""One way to save a fleeting note, whatever the user did to create it (WP-384).

A note can come from typed ".text" (and forwards), from a voice message or from a
Siri / iOS Shortcuts command. All three go through this module, so they check the
same things, write the same file, mark where the note came from and leave the same
trace for the platform. Moving notes to a platform service later means changing
`write_note` only.

- notes_blocker(chat_id): the gate (access -> GitHub connected -> notes repo) as a status.
- write_note(...): append to fleeting-notes.md + trace + metric line; no gate.
- save_fleeting_note(...): gate + write, for callers without their own gate order (voice, Siri).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

logger = logging.getLogger(__name__)

_TRACE_PAYLOAD_LIMIT = 65_536


class NoteSource(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    SIRI = "siri"


# Shown in the note itself (the date line); typed notes stay unmarked, as before.
_SOURCE_LABELS: dict[NoteSource, str] = {
    NoteSource.VOICE: "голос",
    NoteSource.SIRI: "Siri",
}


class NoteStatus(Enum):
    SAVED = "saved"
    NO_ACCESS = "no_access"
    NO_GITHUB = "no_github"
    NO_REPO = "no_repo"
    RECONNECT = "reconnect"
    FAILED = "failed"


@dataclass(frozen=True)
class NoteOutcome:
    status: NoteStatus
    url: str | None = None
    repo: str | None = None
    undo_ref: str | None = None
    write_ref: dict | None = None


async def notes_blocker(telegram_user_id: int) -> NoteStatus | None:
    """Why the user cannot save notes right now, or None when they can."""
    from clients.github_oauth import github_oauth
    from core.access import access_layer

    if not await access_layer.has_access(telegram_user_id, "notes"):
        return NoteStatus.NO_ACCESS
    if not await github_oauth.is_connected(telegram_user_id):
        return NoteStatus.NO_GITHUB
    if not await github_oauth.get_target_repo(telegram_user_id):
        return NoteStatus.NO_REPO
    return None


async def write_note(
    telegram_user_id: int,
    text: str,
    source: NoteSource = NoteSource.TEXT,
    *,
    message_id: int | None = None,
    operation_id: str | None = None,
    target=None,
    created_at=None,
    bind_block_hash=None,
    queue_retry: bool = True,
) -> NoteOutcome:
    """Append the note to the user's fleeting notes, leave the platform trace, log a metric."""
    from clients.github_api import github_notes
    from clients.github_auth import GitHubAuthUnavailable

    started = time.monotonic()

    async def after_attempt(result, attempt_started):
        _finish_note(
            telegram_user_id, text, source, message_id, result, attempt_started
        )

    async def can_retry():
        return await notes_blocker(telegram_user_id) is None

    try:
        result = await github_notes.append_note(
            telegram_user_id,
            text,
            _SOURCE_LABELS.get(source),
            operation_id=operation_id,
            target=target,
            created_at=created_at,
            bind_block_hash=bind_block_hash,
            after_attempt=after_attempt,
            can_retry=can_retry,
            queue_retry=queue_retry,
        )
    except GitHubAuthUnavailable as exc:
        logger.warning(
            "[fleeting_notes] auth unavailable for chat_id=%s: %s",
            telegram_user_id,
            exc,
        )
        outcome = NoteOutcome(NoteStatus.RECONNECT)
        _log_metric(source, outcome.status, len(text), started)
        return outcome
    return _finish_note(telegram_user_id, text, source, message_id, result, started)


def _finish_note(
    telegram_user_id, text, source, message_id, result, started
) -> NoteOutcome:
    """Common completion of an initial write and a queued retry, with the original source."""
    outcome = _saved_outcome(result) if result else NoteOutcome(NoteStatus.FAILED)

    _log_metric(source, outcome.status, len(text), started)
    if outcome.status is NoteStatus.SAVED:
        asyncio.create_task(
            capture_note_trace(
                telegram_user_id,
                text,
                message_id,
                source=source,
                source_ref=result,
            )
        )
    return outcome


async def save_fleeting_note(
    telegram_user_id: int,
    text: str,
    source: NoteSource,
    *,
    message_id: int | None = None,
) -> NoteOutcome:
    """Gate + write, for callers that need nothing between the two (voice, Siri)."""
    started = time.monotonic()
    blocker = await notes_blocker(telegram_user_id)
    if blocker is not None:
        _log_metric(source, blocker, len(text), started)
        return NoteOutcome(blocker)
    return await write_note(telegram_user_id, text, source, message_id=message_id)


def _log_metric(
    source: NoteSource, status: NoteStatus, chars: int, started: float
) -> None:
    """One line per note attempt, to count notes by source and outcome."""
    logger.info(
        "[note-metric] source=%s outcome=%s chars=%d ms=%d",
        source.value,
        status.value,
        chars,
        (time.monotonic() - started) * 1000,
    )


def _saved_outcome(result: dict) -> NoteOutcome:
    from clients.github_api import NoteRef

    repo = result["repo"]
    branch = result.get("branch", "main")
    undo_ref = None
    if result.get("content_matches", True) and all(
        result.get(key) for key in ("operation_id", "block_hash", "target_hash")
    ):
        undo_ref = NoteRef(
            result["operation_id"], result["block_hash"], result["target_hash"]
        ).encode()
    return NoteOutcome(
        NoteStatus.SAVED,
        url=f"https://github.com/{repo}/blob/{branch}/{result['path']}",
        repo=repo,
        undo_ref=undo_ref,
        write_ref=result,
    )


# --- platform trace (WP-427, sensor bot_note) ---------------------------------


def build_note_payload(
    note_text: str, source: NoteSource, source_ref: dict | None
) -> dict:
    """Trace content respecting the 64 KiB limit: full text, or the reference only."""
    base: dict = {"source": source.value}
    if source_ref:
        base["source_ref"] = {
            "type": "github_blob",
            "repo": source_ref.get("repo"),
            "path": source_ref.get("path"),
            "sha": source_ref.get("sha"),
            "branch": source_ref.get("branch"),
            "line_start": source_ref.get("line_start"),
            "operation_id": source_ref.get("operation_id"),
        }

    if source_ref and (
        not source_ref.get("content_matches", True)
        or not source_ref.get("input_matches", True)
    ):
        return {**base, "text": None}
    candidate = {**base, "text": note_text}
    size = len(json.dumps(candidate, ensure_ascii=False).encode("utf-8"))
    if size <= _TRACE_PAYLOAD_LIMIT:
        return candidate

    logger.info(
        "bot_note payload oversized (%d bytes); falling back to reference-only for note",
        size,
    )
    return {**base, "text": None}


async def capture_note_trace(
    telegram_user_id: int,
    note_text: str,
    message_id: int | None,
    *,
    source: NoteSource = NoteSource.TEXT,
    source_ref: dict | None = None,
) -> None:
    """Best-effort fire-and-forget: record the saved note as a trace for the platform.

    A repeated operation keeps its external id. Two separate Siri notes have distinct
    operation ids even when their text and timestamp are identical.
    """
    try:
        from clients.gateway_mcp import gateway_mcp

        if not gateway_mcp.is_connected(telegram_user_id):
            logger.info(
                "capture_trace(bot_note) skipped: user %s not connected to Aisystant",
                telegram_user_id,
            )
            return

        operation_id = (source_ref or {}).get("operation_id")
        if operation_id:
            external_id = f"note_{operation_id}"
        elif message_id is not None:
            external_id = f"tg_note_{message_id}"
        else:
            external_id = f"{source.value}_note_{uuid4().hex}"

        await gateway_mcp.capture_trace(
            sensor_id="bot_note",
            event_type="note_created",
            content=build_note_payload(note_text, source, source_ref),
            telegram_user_id=telegram_user_id,
            external_id=external_id,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort trace must not undo a saved note
        logger.warning(
            "capture_trace(bot_note) failed for user %s: %s",
            telegram_user_id,
            type(exc).__name__,
        )
