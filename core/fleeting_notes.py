"""Saving a fleeting note for a user outside a chat message (WP-384).

The same checks and the same write as a typed ".text" note
(handlers/github.py::handle_fleeting_note): notes access, GitHub connection,
selected notes repo, then append to fleeting-notes.md. Used by the notes
shortcut endpoint (Siri / iOS Shortcuts), where there is no chat message to reply to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


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


async def save_fleeting_note(telegram_user_id: int, text: str) -> NoteOutcome:
    """Append `text` to the user's fleeting notes; the outcome says what happened."""
    from clients.github_api import github_notes
    from clients.github_auth import GitHubAuthUnavailable
    from clients.github_oauth import github_oauth
    from core.access import access_layer

    if not await access_layer.has_access(telegram_user_id, "notes"):
        return NoteOutcome(NoteStatus.NO_ACCESS)
    if not await github_oauth.is_connected(telegram_user_id):
        return NoteOutcome(NoteStatus.NO_GITHUB)
    if not await github_oauth.get_target_repo(telegram_user_id):
        return NoteOutcome(NoteStatus.NO_REPO)
    try:
        result = await github_notes.append_note(telegram_user_id, text)
    except GitHubAuthUnavailable as exc:
        logger.warning("[fleeting_notes] auth unavailable for chat_id=%s: %s", telegram_user_id, exc)
        return NoteOutcome(NoteStatus.RECONNECT)
    if not result:
        return NoteOutcome(NoteStatus.FAILED)
    branch = result.get("branch", "main")
    repo = result["repo"]
    return NoteOutcome(
        NoteStatus.SAVED,
        url=f"https://github.com/{repo}/blob/{branch}/{result['path']}",
        repo=repo,
    )
