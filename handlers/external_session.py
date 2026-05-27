# see DP.SC.162, DP.ROLE.061
"""
External Session Ingress (WP-358).

Telegram → GitHub (session files) → launchd git pull →
dispatcher --mode session → claude -p → Telegram.

Команды:
- /claude <text>  — открыть сессию (или добавить ход в активную)
- /close          — завершить сессию (status: completed)
- /cancel         — отменить сессию (status: cancelled)

Обычное сообщение при активной сессии → добавляет ход в тред.

Env vars:
- GITHUB_SESSION_PAT      — PAT с write-доступом к GITHUB_SESSION_REPO
- GITHUB_SESSION_REPO     — TserenTserenov/DS-my-strategy
- SESSION_ALLOWED_CHAT_IDS — comma-separated chat_id (пустое = разрешено всем)
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

external_session_router = Router(name="external_session")

# ── Env vars ──────────────────────────────────────────────────────────────────

_GITHUB_PAT = os.getenv("GITHUB_SESSION_PAT", "")
_GITHUB_REPO = os.getenv("GITHUB_SESSION_REPO", "TserenTserenov/DS-my-strategy")
_SESSIONS_PATH = "inbox/agent/sessions"
_BRANCH = "main"

_ALLOWED_CHAT_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("SESSION_ALLOWED_CHAT_IDS", "").split(",")
    if x.strip().isdigit()
}

# ── In-memory state (MVP, single-pilot, resets on restart) ───────────────────

_active_sessions: dict[int, str] = {}        # chat_id → session_id
_turn_counts: dict[tuple[int, str], int] = {}  # (chat_id, session_id) → last turn_n

# ── GitHub API helpers ────────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {_GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _gh_get_file(path: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (decoded_content, sha) or (None, None) on miss/error."""
    url = f"https://api.github.com/repos/{_GITHUB_REPO}/contents/{path}"
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, headers=_gh_headers(), params={"ref": _BRANCH}) as resp:
            if resp.status == 404:
                return None, None
            if resp.status != 200:
                logger.error("[session] GET %s → %d", path, resp.status)
                return None, None
            data = await resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]


async def _gh_put_file(path: str, content: str, msg: str, sha: Optional[str] = None) -> bool:
    """Create (sha=None) or update (sha=existing) file. Returns True on success."""
    url = f"https://api.github.com/repos/{_GITHUB_REPO}/contents/{path}"
    body: dict = {
        "message": msg,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": _BRANCH,
    }
    if sha:
        body["sha"] = sha
    async with aiohttp.ClientSession() as sess:
        async with sess.put(url, headers=_gh_headers(), json=body) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.error("[session] PUT %s → %d: %s", path, resp.status, text[:200])
                return False
            return True

# ── Session file helpers ──────────────────────────────────────────────────────

def _new_session_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    uid = secrets.token_hex(3)  # 6 hex chars
    return f"SESSION-{ts}-{uid}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _meta_content(session_id: str, tg_chat_id: int, now: str, turn_count: int = 1) -> str:
    return (
        f"---\n"
        f"session_id: {session_id}\n"
        f"tg_chat_id: {tg_chat_id}\n"
        f"created_at: {now}\n"
        f"last_turn_at: {now}\n"
        f"status: active\n"
        f"private: false\n"
        f"turn_count: {turn_count}\n"
        f"---\n"
    )


def _turn_line(turn_n: int, tg_msg_id: int, now: str) -> str:
    return f"[turn:{turn_n}, role:pilot, tg_msg_id:{tg_msg_id}, ts:{now}]\n"


async def _create_session(tg_chat_id: int, tg_msg_id: int, text: str) -> Optional[str]:
    """Create SESSION-<id>.md + SESSION-<id>-thread.md. Returns session_id or None."""
    session_id = _new_session_id()
    now = _now_iso()
    meta_path = f"{_SESSIONS_PATH}/{session_id}.md"
    thread_path = f"{_SESSIONS_PATH}/{session_id}-thread.md"

    ok1 = await _gh_put_file(
        meta_path,
        _meta_content(session_id, tg_chat_id, now),
        f"session: open {session_id}",
    )
    if not ok1:
        return None

    thread_body = _turn_line(1, tg_msg_id, now) + text + "\n\n"
    ok2 = await _gh_put_file(
        thread_path,
        thread_body,
        f"session: turn 1 pilot {session_id}",
    )
    if not ok2:
        return None

    return session_id


async def _append_pilot_turn(session_id: str, tg_msg_id: int, text: str, turn_n: int) -> bool:
    """Append pilot turn to thread; update meta last_turn_at + turn_count."""
    now = _now_iso()
    thread_path = f"{_SESSIONS_PATH}/{session_id}-thread.md"
    meta_path = f"{_SESSIONS_PATH}/{session_id}.md"

    cur_thread, thread_sha = await _gh_get_file(thread_path)
    if cur_thread is None:
        logger.error("[session] thread not found: %s", session_id)
        return False

    new_thread = cur_thread + _turn_line(turn_n, tg_msg_id, now) + text + "\n\n"
    ok = await _gh_put_file(
        thread_path, new_thread,
        f"session: turn {turn_n} pilot {session_id}",
        thread_sha,
    )
    if not ok:
        return False

    cur_meta, meta_sha = await _gh_get_file(meta_path)
    if cur_meta is not None:
        new_meta = re.sub(r"last_turn_at:.*", f"last_turn_at: {now}", cur_meta)
        new_meta = re.sub(r"turn_count:.*", f"turn_count: {turn_n}", new_meta)
        await _gh_put_file(meta_path, new_meta, f"session: meta turn {turn_n} {session_id}", meta_sha)

    return True


async def _set_session_status(session_id: str, status: str) -> bool:
    meta_path = f"{_SESSIONS_PATH}/{session_id}.md"
    cur_meta, meta_sha = await _gh_get_file(meta_path)
    if cur_meta is None:
        return False
    new_meta = re.sub(r"status:.*", f"status: {status}", cur_meta)
    return await _gh_put_file(meta_path, new_meta, f"session: {status} {session_id}", meta_sha)

# ── Access control ────────────────────────────────────────────────────────────

def _allowed(chat_id: int) -> bool:
    return not _ALLOWED_CHAT_IDS or chat_id in _ALLOWED_CHAT_IDS


def _has_active_session(message: Message) -> bool:
    return message.chat.id in _active_sessions

# ── Handlers ──────────────────────────────────────────────────────────────────

@external_session_router.message(Command("claude"))
async def cmd_claude(message: Message) -> None:
    """/claude <text> — open or continue a session."""
    if not _allowed(message.chat.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Напишите запрос: /claude <текст>")
        return

    user_text = parts[1].strip()
    chat_id = message.chat.id

    if chat_id in _active_sessions:
        # Continue existing session
        session_id = _active_sessions[chat_id]
        turn_n = _turn_counts.get((chat_id, session_id), 1) + 1
        _turn_counts[(chat_id, session_id)] = turn_n
        ok = await _append_pilot_turn(session_id, message.message_id, user_text, turn_n)
        if ok:
            await message.answer("⏳ Работаю...")
        else:
            await message.answer("Ошибка при записи хода. Попробуй ещё раз.")
        return

    # New session
    await message.answer("⏳ Открываю сессию...")
    session_id = await _create_session(chat_id, message.message_id, user_text)
    if session_id:
        _active_sessions[chat_id] = session_id
        _turn_counts[(chat_id, session_id)] = 1
        await message.answer(
            f"Сессия открыта: {session_id}\n"
            "Claude обработает запрос и ответит здесь."
        )
    else:
        await message.answer("Не удалось создать сессию. Проверь конфигурацию (GITHUB_SESSION_PAT).")


@external_session_router.message(_has_active_session, F.text)
async def handle_session_text(message: Message) -> None:
    """Text messages in active sessions → append turn."""
    chat_id = message.chat.id
    if not _allowed(chat_id):
        return

    text = message.text or ""
    # Commands handled by their own handlers; skip here to avoid double-processing
    if text.startswith("/"):
        return

    session_id = _active_sessions[chat_id]
    turn_n = _turn_counts.get((chat_id, session_id), 1) + 1
    _turn_counts[(chat_id, session_id)] = turn_n

    ok = await _append_pilot_turn(session_id, message.message_id, text, turn_n)
    if ok:
        await message.answer("⏳ Работаю...")
    else:
        await message.answer("Ошибка при записи хода. Попробуй ещё раз.")


@external_session_router.message(Command("close"))
async def cmd_close(message: Message) -> None:
    """/close — complete active session."""
    chat_id = message.chat.id
    if not _allowed(chat_id):
        return

    session_id = _active_sessions.pop(chat_id, None)
    if not session_id:
        await message.answer("Нет активной сессии.")
        return

    _turn_counts.pop((chat_id, session_id), None)
    ok = await _set_session_status(session_id, "completed")
    if ok:
        await message.answer(f"Сессия {session_id} завершена.")
    else:
        await message.answer("Сессия закрыта локально (GitHub недоступен).")


@external_session_router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    """/cancel — cancel active session."""
    chat_id = message.chat.id
    if not _allowed(chat_id):
        return

    session_id = _active_sessions.pop(chat_id, None)
    if not session_id:
        await message.answer("Нет активной сессии.")
        return

    _turn_counts.pop((chat_id, session_id), None)
    ok = await _set_session_status(session_id, "cancelled")
    if ok:
        await message.answer(f"Сессия {session_id} отменена.")
    else:
        await message.answer("Сессия отменена локально (GitHub недоступен).")
