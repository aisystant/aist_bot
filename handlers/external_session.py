# see DP.SC.162, DP.ROLE.061
"""
External Session Ingress (WP-358, Ф7: multi-user via per-user GitHub OAuth).

Telegram → GitHub (session files) → launchd git pull →
dispatcher --mode session → claude -p → Telegram.

Команды:
- /claude <text>  — открыть сессию (или добавить ход в активную)
- /close          — завершить сессию (status: completed)
- /cancel         — отменить сессию (status: cancelled)

Обычное сообщение при активной сессии → добавляет ход в тред.

Env vars (пилот-fallback, опционально):
- GITHUB_SESSION_PAT       — PAT для fallback если DB недоступна
- GITHUB_SESSION_REPO      — репо для fallback (owner/repo)
- SESSION_ALLOWED_CHAT_IDS — comma-separated chat_id для pilot-fallback

Все T4-пользователи (с GitHub OAuth) используют per-user токен из github_connections.
"""
from __future__ import annotations

import asyncio
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

# ── Env vars (pilot fallback only) ───────────────────────────────────────────

_PILOT_PAT = os.getenv("GITHUB_SESSION_PAT", "")
_PILOT_REPO = os.getenv("GITHUB_SESSION_REPO", "")
_SESSIONS_PATH = "inbox/agent/sessions"

_ALLOWED_CHAT_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("SESSION_ALLOWED_CHAT_IDS", "").split(",")
    if x.strip().isdigit()
}

# ── In-memory state (MVP, single-pilot, resets on restart) ───────────────────

_active_sessions: dict[int, str] = {}        # chat_id → session_id
_turn_counts: dict[tuple[int, str], int] = {}  # (chat_id, session_id) → last turn_n

# ── GitHub credentials helpers ────────────────────────────────────────────────

def _normalize_repo(repo: str) -> str:
    """Normalize 'https://github.com/owner/repo.git' → 'owner/repo'."""
    repo = repo.strip()
    for prefix in ("https://github.com/", "http://github.com/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo


async def _get_github_creds(chat_id: int) -> Optional[tuple[str, str, str]]:
    """Return (token, repo, branch) for chat_id, or None if not configured.

    Order: DB per-user first (T4 OAuth), then pilot env fallback.
    Token is never logged.
    """
    # 1. Per-user DB lookup (all users including pilot)
    try:
        from db.queries.github import get_github_connection
        conn = await asyncio.wait_for(get_github_connection(chat_id), timeout=5.0)
        if conn and conn.get("access_token") and conn.get("strategy_repo"):
            repo = _normalize_repo(conn["strategy_repo"])
            branch = conn.get("strategy_default_branch") or "main"
            return conn["access_token"], repo, branch
    except asyncio.TimeoutError:
        logger.error("[session] DB timeout for chat_id=%s", chat_id)
    except Exception as e:
        logger.error("[session] DB error for chat_id=%s: %s", chat_id, type(e).__name__)

    # 2. Pilot env fallback — only for explicitly allowed chat_ids
    if _PILOT_PAT and _PILOT_REPO and _ALLOWED_CHAT_IDS and chat_id in _ALLOWED_CHAT_IDS:
        return _PILOT_PAT, _normalize_repo(_PILOT_REPO), "main"

    return None


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _gh_get_file(
    path: str, token: str, repo: str, branch: str
) -> tuple[Optional[str], Optional[str]]:
    """Returns (decoded_content, sha) or (None, None) on miss/error."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, headers=_gh_headers(token), params={"ref": branch}) as resp:
            if resp.status == 404:
                return None, None
            if resp.status == 401:
                logger.warning("[session] GitHub 401 for path=%s — token expired?", path)
                return None, None
            if resp.status != 200:
                logger.error("[session] GET %s → %d", path, resp.status)
                return None, None
            data = await resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]


async def _gh_put_file(
    path: str, content: str, msg: str, token: str, repo: str, branch: str,
    sha: Optional[str] = None,
) -> bool:
    """Create (sha=None) or update (sha=existing) file. Retries once on 422 (sha conflict)."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    async def _attempt(current_sha: Optional[str]) -> int:
        body: dict = {
            "message": msg,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if current_sha:
            body["sha"] = current_sha
        async with aiohttp.ClientSession() as sess:
            async with sess.put(url, headers=_gh_headers(token), json=body) as resp:
                return resp.status

    status = await _attempt(sha)
    if status in (200, 201):
        return True

    # Retry on 422 (SHA conflict): re-fetch current sha and retry once
    if status == 422:
        logger.warning("[session] PUT %s → 422 SHA conflict, retrying", path)
        _, fresh_sha = await _gh_get_file(path, token, repo, branch)
        if fresh_sha is None:
            # File doesn't exist yet — 422 on create is unexpected, don't retry
            logger.error("[session] PUT %s → 422 but file not found, cannot retry", path)
        else:
            status = await _attempt(fresh_sha)
            if status in (200, 201):
                return True

    if status == 401:
        logger.warning("[session] PUT %s → 401 token expired", path)
    elif status == 403:
        logger.warning("[session] PUT %s → 403 (rate-limit or permissions)", path)
    else:
        logger.error("[session] PUT %s → %d", path, status)
    return False

# ── Session folder bootstrap ──────────────────────────────────────────────────

_SESSIONS_SPEC = """\
# inbox/agent/sessions

Папка для сессий внешнего канала IWE (/claude из Telegram).

Структура файлов сессии:
- SESSION-<id>.md          — метаданные (status, turn_count, tg_chat_id)
- SESSION-<id>-thread.md   — тред сообщений (ходы пилота и Claude)

See: DP.SC.162, DP.ROLE.061 (WP-358)
"""


async def _ensure_sessions_folder(token: str, repo: str, branch: str) -> None:
    """Create inbox/agent/sessions/SPEC.md if folder doesn't exist yet."""
    spec_path = f"{_SESSIONS_PATH}/SPEC.md"
    content, _ = await _gh_get_file(spec_path, token, repo, branch)
    if content is not None:
        return  # already exists
    ok = await _gh_put_file(
        spec_path, _SESSIONS_SPEC,
        "chore: init inbox/agent/sessions (WP-358)",
        token, repo, branch,
    )
    if ok:
        logger.info("[session] created %s in %s", spec_path, repo)
    else:
        logger.warning("[session] could not create sessions folder in %s", repo)


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


async def _create_session(
    tg_chat_id: int, tg_msg_id: int, text: str,
    token: str, repo: str, branch: str,
) -> Optional[str]:
    """Create SESSION-<id>.md + SESSION-<id>-thread.md. Returns session_id or None."""
    await _ensure_sessions_folder(token, repo, branch)

    session_id = _new_session_id()
    now = _now_iso()
    meta_path = f"{_SESSIONS_PATH}/{session_id}.md"
    thread_path = f"{_SESSIONS_PATH}/{session_id}-thread.md"

    ok1 = await _gh_put_file(
        meta_path,
        _meta_content(session_id, tg_chat_id, now),
        f"session: open {session_id}",
        token, repo, branch,
    )
    if not ok1:
        return None

    thread_body = _turn_line(1, tg_msg_id, now) + text + "\n\n"
    ok2 = await _gh_put_file(
        thread_path, thread_body,
        f"session: turn 1 pilot {session_id}",
        token, repo, branch,
    )
    if not ok2:
        return None

    return session_id


async def _append_pilot_turn(
    session_id: str, tg_msg_id: int, text: str, turn_n: int,
    token: str, repo: str, branch: str,
) -> bool:
    """Append pilot turn to thread; update meta last_turn_at + turn_count."""
    now = _now_iso()
    thread_path = f"{_SESSIONS_PATH}/{session_id}-thread.md"
    meta_path = f"{_SESSIONS_PATH}/{session_id}.md"

    cur_thread, thread_sha = await _gh_get_file(thread_path, token, repo, branch)
    if cur_thread is None:
        logger.error("[session] thread not found: %s", session_id)
        return False

    new_thread = cur_thread + _turn_line(turn_n, tg_msg_id, now) + text + "\n\n"
    ok = await _gh_put_file(
        thread_path, new_thread,
        f"session: turn {turn_n} pilot {session_id}",
        token, repo, branch, thread_sha,
    )
    if not ok:
        return False

    cur_meta, meta_sha = await _gh_get_file(meta_path, token, repo, branch)
    if cur_meta is not None:
        new_meta = re.sub(r"last_turn_at:.*", f"last_turn_at: {now}", cur_meta)
        new_meta = re.sub(r"turn_count:.*", f"turn_count: {turn_n}", new_meta)
        ok_meta = await _gh_put_file(
            meta_path, new_meta,
            f"session: meta turn {turn_n} {session_id}",
            token, repo, branch, meta_sha,
        )
        if not ok_meta:
            logger.warning("[session] meta update failed for %s turn %d", session_id, turn_n)

    return True


async def _set_session_status(
    session_id: str, status: str, token: str, repo: str, branch: str,
) -> bool:
    meta_path = f"{_SESSIONS_PATH}/{session_id}.md"
    cur_meta, meta_sha = await _gh_get_file(meta_path, token, repo, branch)
    if cur_meta is None:
        return False
    new_meta = re.sub(r"status:.*", f"status: {status}", cur_meta)
    return await _gh_put_file(
        meta_path, new_meta,
        f"session: {status} {session_id}",
        token, repo, branch, meta_sha,
    )

# ── Filters ───────────────────────────────────────────────────────────────────

def _has_active_session(message: Message) -> bool:
    return message.chat.id in _active_sessions

# ── Handlers ──────────────────────────────────────────────────────────────────

@external_session_router.message(Command("claude"))
async def cmd_claude(message: Message) -> None:
    """/claude <text> — open or continue a session."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Напишите запрос: /claude <текст>")
        return

    user_text = parts[1].strip()
    chat_id = message.chat.id

    creds = await _get_github_creds(chat_id)
    if not creds:
        await message.answer(
            "Для команды /claude нужно подключить GitHub и указать репо стратегии.\n"
            "Откройте /settings → GitHub → Strategy repo."
        )
        return

    token, repo, branch = creds

    if chat_id in _active_sessions:
        session_id = _active_sessions[chat_id]
        turn_n = _turn_counts.get((chat_id, session_id), 1) + 1
        _turn_counts[(chat_id, session_id)] = turn_n
        ok = await _append_pilot_turn(session_id, message.message_id, user_text, turn_n, token, repo, branch)
        if ok:
            await message.answer("⏳ Работаю...")
        else:
            await message.answer("Ошибка при записи хода. Попробуй ещё раз.")
        return

    await message.answer("⏳ Открываю сессию...")
    session_id = await _create_session(chat_id, message.message_id, user_text, token, repo, branch)
    if session_id:
        _active_sessions[chat_id] = session_id
        _turn_counts[(chat_id, session_id)] = 1
        await message.answer(
            f"Сессия открыта: {session_id}\n"
            "Claude обработает запрос и ответит здесь."
        )
    else:
        await message.answer(
            "Не удалось создать сессию. Проверь подключение GitHub в /settings.\n"
            "Если токен истёк — переподключите GitHub."
        )


@external_session_router.message(_has_active_session, F.text)
async def handle_session_text(message: Message) -> None:
    """Text messages in active sessions → append turn."""
    chat_id = message.chat.id
    text = message.text or ""
    if text.startswith("/"):
        return

    creds = await _get_github_creds(chat_id)
    if not creds:
        await message.answer("GitHub не подключён. Открой /settings → GitHub.")
        return

    token, repo, branch = creds
    session_id = _active_sessions[chat_id]
    turn_n = _turn_counts.get((chat_id, session_id), 1) + 1
    _turn_counts[(chat_id, session_id)] = turn_n

    ok = await _append_pilot_turn(session_id, message.message_id, text, turn_n, token, repo, branch)
    if ok:
        await message.answer("⏳ Работаю...")
    else:
        await message.answer("Ошибка при записи хода. Попробуй ещё раз.")


@external_session_router.message(Command("close"))
async def cmd_close(message: Message) -> None:
    """/close — complete active session."""
    chat_id = message.chat.id
    session_id = _active_sessions.pop(chat_id, None)
    if not session_id:
        await message.answer("Нет активной сессии.")
        return

    _turn_counts.pop((chat_id, session_id), None)
    creds = await _get_github_creds(chat_id)
    if creds:
        ok = await _set_session_status(session_id, "completed", *creds)
        if ok:
            await message.answer(f"Сессия {session_id} завершена.")
            return
    await message.answer("Сессия закрыта локально (GitHub недоступен).")


@external_session_router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    """/cancel — cancel active session."""
    chat_id = message.chat.id
    session_id = _active_sessions.pop(chat_id, None)
    if not session_id:
        await message.answer("Нет активной сессии.")
        return

    _turn_counts.pop((chat_id, session_id), None)
    creds = await _get_github_creds(chat_id)
    if creds:
        ok = await _set_session_status(session_id, "cancelled", *creds)
        if ok:
            await message.answer(f"Сессия {session_id} отменена.")
            return
    await message.answer("Сессия отменена локально (GitHub недоступен).")
