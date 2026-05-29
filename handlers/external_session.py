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
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

logger = logging.getLogger(__name__)

external_session_router = Router(name="external_session")


# WP-358 Ф10 (peer-session 2026-05-28-08): aiogram FSM-стейт для /claude сессии.
# Заменяет in-memory dict `_active_sessions` + `_turn_counts`. Persistence через
# PostgresStorage (bot.py:304). State data schema:
#   session_id: str         — SESSION-YYYYMMDD-HHMMSS-XXXXXX
#   turn_count: int         — последний пилот-ход
#   last_turn_at: str       — ISO-8601 timestamp последнего хода (для auto-close)
class ExternalSession(StatesGroup):
    active = State()


# Auto-close timeout для зависших /claude сессий (WP-358 Ф10 B-β).
# При входе в handle_session_text — если last_turn_at старше этого порога,
# state.clear() + сообщение пилоту, обработка как новой сессии.
_SESSION_IDLE_TIMEOUT = timedelta(minutes=30)

# ── Env vars (pilot fallback only) ───────────────────────────────────────────

_PILOT_PAT = os.getenv("GITHUB_SESSION_PAT", "")
_PILOT_REPO = os.getenv("GITHUB_SESSION_REPO", "")
_SESSIONS_PATH = "inbox/agent/sessions"

_ALLOWED_CHAT_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("SESSION_ALLOWED_CHAT_IDS", "").split(",")
    if x.strip().isdigit()
}

# Fix 2 (peer-session 2026-05-28-05): SM-mutex with timeout.
# Marathon SM использует ОТДЕЛЬНУЮ state machine (development.user_state.current_state),
# не aiogram FSM — поэтому unification через aiogram FSM не даёт free mutex с marathon.
# Этот guard остаётся как явная проверка: если marathon ждёт ответ на свой
# последний вопрос (recent timestamp), свободный текст уходит в fallback → SM.
# Импортируется из config — см. WP-358 Ф10 Op-5.
try:
    from config.settings import SM_EXPECTING_REPLY_STATES as _SM_EXPECTING_REPLY_STATES
except ImportError:
    # Fallback для совместимости при первом deploy (до config.settings.py изменений)
    _SM_EXPECTING_REPLY_STATES: dict[str, int] = {
        "workshop.marathon.question": 60,    # ждём ответ на вопрос ≤60 мин
        "workshop.marathon.task": 1440,      # задание может выполняться до 24ч
        "workshop.marathon.bonus": 60,
        "workshop.assessment.flow": 60,
    }

# ── GitHub credentials helpers ────────────────────────────────────────────────

def _normalize_repo(repo: str) -> str:
    """Normalize 'https://github.com/owner/repo.git/' → 'owner/repo'."""
    repo = repo.strip()
    for prefix in ("https://github.com/", "http://github.com/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
    repo = repo.rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo.rstrip("/")


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
        # Ф9: сбрасываем статус в pending если не processing — диспетчер подберёт новый ход.
        # Regex tolerant к обоим форматам: `status: processing` (Mac, _yaml_repr)
        # и `status: "processing"` (цех-1, update_frontmatter). Якоря ^...$ + MULTILINE
        # исключают ложный матч на `status: processing-extra`; `\s*$` устойчив к
        # trailing whitespace. re.sub с якорями защищает от substring-matches
        # на будущих полях типа `last_status:`.
        # TODO(WP-358 backlog): унифицировать формат status между диспетчерами
        # (см. archive/wp-contexts/WP-358-external-session-ingress.md → Открытые задачи Ф7).
        if not re.search(r'^status:\s*"?processing"?\s*$', cur_meta, re.MULTILINE):
            new_meta = re.sub(r'^status:.*$', 'status: pending', new_meta, flags=re.MULTILINE)
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

# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso_for_state() -> str:
    """ISO timestamp for FSM data — без микросекунд для парс-консистентности."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _sm_is_expecting_reply(chat_id: int) -> bool:
    """Return True if SM or marathon scheduler is expecting a free-text reply.

    Two checks:
    1. SM-mutex (Fix 2, peer-session 2026-05-28-05): development.user_state.current_state
       is in _SM_EXPECTING_REPLY_STATES and updated_at is within timeout.
    2. Marathon-queue guard (Fix 4, peer-session 2026-05-28-12): lesson_practice was
       recently sent by MarathonQueue (which bypasses SM transitions). If a lesson_practice
       was delivered within the last 60 min, the user's reply belongs to the marathon, not /claude.

    fail-open: при ошибках → False (пропустить в /claude, не блокировать).
    """
    # Check 1: SM state (old marathon via State Machine, and any SM-based flows)
    try:
        from db.queries import get_intern
        intern = await get_intern(chat_id)
        if intern:
            current_state = intern.get("current_state") or ""
            if current_state in _SM_EXPECTING_REPLY_STATES:
                updated_at = intern.get("updated_at")
                if updated_at is not None:
                    timeout_min = _SM_EXPECTING_REPLY_STATES[current_state]
                    now = datetime.now(timezone.utc)
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    age_sec = (now - updated_at).total_seconds()
                    if age_sec < timeout_min * 60:
                        return True
    except Exception as exc:
        logger.warning("[session] _sm_is_expecting_reply SM-check failed for %s: %s", chat_id, exc)

    # Check 2: WP-330 marathon scheduler — delivers lesson_practice bypassing SM.
    # If lesson_practice was sent recently, the user's reply belongs to marathon.
    try:
        from db.queries.marathon_newcomer import has_recent_lesson_practice_sent
        if await has_recent_lesson_practice_sent(chat_id, within_minutes=60):
            logger.info("[session] Marathon lesson_practice sent recently for chat %s — skipping to fallback", chat_id)
            return True
    except Exception as exc:
        logger.warning("[session] _sm_is_expecting_reply marathon-check failed for %s: %s", chat_id, exc)

    return False

# ── Handlers ──────────────────────────────────────────────────────────────────

async def _get_session_data(state: FSMContext) -> Optional[dict]:
    """Safe FSM data read — None при StorageError."""
    try:
        return await state.get_data()
    except Exception as exc:
        logger.error("[session] FSM get_data error: %s", type(exc).__name__)
        return None


async def _open_new_session(
    message: Message, state: FSMContext, user_text: str,
    token: str, repo: str, branch: str,
) -> None:
    """Создать новую /claude сессию + установить FSM state."""
    await message.answer("⏳ Открываю сессию...")
    chat_id = message.chat.id
    session_id = await _create_session(chat_id, message.message_id, user_text, token, repo, branch)
    if not session_id:
        await message.answer(
            "Не удалось создать сессию. Проверь подключение GitHub в /settings.\n"
            "Если токен истёк — переподключите GitHub."
        )
        return
    try:
        await state.set_state(ExternalSession.active)
        await state.update_data(
            session_id=session_id,
            turn_count=1,
            last_turn_at=_now_iso_for_state(),
        )
    except Exception as exc:
        logger.error("[session] FSM set_state failed for %s: %s", chat_id, type(exc).__name__)
        await message.answer(
            f"Сессия создана ({session_id}), но не удалось сохранить state. "
            "Возможно проблемы с БД — попробуйте через минуту."
        )
        return
    await message.answer(
        f"Сессия открыта: {session_id}\n"
        "Claude обработает запрос и ответит здесь."
    )


@external_session_router.message(Command("claude"))
async def cmd_claude(message: Message, state: FSMContext) -> None:
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

    # Проверка существующей сессии через FSM
    try:
        current_state = await state.get_state()
    except Exception as exc:
        logger.error("[session] FSM get_state failed for %s: %s", chat_id, type(exc).__name__)
        await message.answer("Не удалось прочитать состояние сессии. Попробуйте через минуту.")
        return

    if current_state == ExternalSession.active.state:
        data = await _get_session_data(state)
        if data and data.get("session_id"):
            session_id = data["session_id"]
            # B-β auto-close симметрия: /claude после >30min idle тоже триггерит closure
            last_turn_at_str = data.get("last_turn_at")
            if last_turn_at_str:
                try:
                    last_dt = datetime.fromisoformat(last_turn_at_str.replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) - last_dt > _SESSION_IDLE_TIMEOUT:
                        logger.info("[session] /claude after timeout for chat %s — closing %s + new", chat_id, session_id)
                        if not await _set_session_status(session_id, "completed", token, repo, branch):
                            logger.warning("[session] Orphan possible: %s could not be marked completed", session_id)
                        try:
                            await state.clear()
                        except Exception:
                            pass
                        await message.answer(f"Прошлая сессия ({session_id}) закрыта по таймауту. Открываю новую.")
                        await _open_new_session(message, state, user_text, token, repo, branch)
                        return
                except (ValueError, TypeError):
                    pass  # fail-open
            turn_n = int(data.get("turn_count", 0)) + 1
            try:
                await state.update_data(turn_count=turn_n, last_turn_at=_now_iso_for_state())
            except Exception as exc:
                logger.error("[session] FSM update_data failed: %s", type(exc).__name__)
                await message.answer("Не удалось обновить состояние. Попробуйте ещё раз.")
                return
            ok = await _append_pilot_turn(session_id, message.message_id, user_text, turn_n, token, repo, branch)
            if ok:
                await message.answer("⏳ Работаю...")
            else:
                await message.answer("Ошибка при записи хода. Попробуй ещё раз.")
            return
        # Corrupted state: state=active без session_id → clear и создать новую
        logger.warning("[session] Corrupted state for chat %s — clearing", chat_id)
        try:
            await state.clear()
        except Exception:
            pass

    await _open_new_session(message, state, user_text, token, repo, branch)


@external_session_router.message(
    StateFilter(ExternalSession.active),
    F.text,
    ~F.text.startswith("/"),
)
async def handle_session_text(message: Message, state: FSMContext) -> None:
    """Text messages in active /claude sessions → append turn.

    Activates only при FSM state == ExternalSession.active AND текст не команда
    (StateFilter + ~startswith / в фильтре, чтобы slash-команды НЕ попадали сюда
    и могли быть обработаны своими роутерами — иначе `return` в aiogram 3
    считается «handled» и блокирует пропагацию).

    Edge cases:
      - corrupted state (active без session_id) → clear + сообщение
      - SM marathon ждёт ответ → SkipHandler → fallback → marathon SM
      - last_turn_at >30 мин → auto-close + новая сессия из текущего сообщения
    """
    chat_id = message.chat.id
    text = message.text or ""

    # SM-mutex (Fix 2 из 05-сессии): marathon SM использует отдельную state machine
    # (development.user_state.current_state, не aiogram FSM) — guard остаётся явный.
    # SkipHandler позволяет aiogram продолжить propagation к fallback_router.
    if await _sm_is_expecting_reply(chat_id):
        logger.info("[session] SM expecting reply for chat %s — skipping to fallback", chat_id)
        raise SkipHandler

    data = await _get_session_data(state)
    if data is None:
        await message.answer("Сервис временно недоступен (storage). Попробуйте через минуту.")
        return

    session_id = data.get("session_id")
    if not session_id:
        # Corrupted state — active без session_id
        logger.error("[session] Corrupted state for chat %s — clearing", chat_id)
        try:
            await state.clear()
        except Exception:
            pass
        await message.answer("Сессия повреждена. Начните заново: /claude <запрос>")
        return

    # B-β auto-close: если last_turn_at старше _SESSION_IDLE_TIMEOUT — закрыть и начать новую.
    # Порядок важен: сначала проверяем creds, потом анонсируем закрытие, иначе пилот
    # получит «прошлая сессия закрыта» + сразу следом «GitHub не подключён».
    last_turn_at_str = data.get("last_turn_at")
    auto_closed = False
    if last_turn_at_str:
        try:
            last_dt = datetime.fromisoformat(last_turn_at_str.replace("Z", "+00:00"))
            auto_closed = datetime.now(timezone.utc) - last_dt > _SESSION_IDLE_TIMEOUT
        except (ValueError, TypeError) as exc:
            logger.warning("[session] Failed to parse last_turn_at=%r: %s", last_turn_at_str, exc)
            # fail-open: считаем что не таймаут

    if auto_closed:
        creds = await _get_github_creds(chat_id)
        if not creds:
            # Не можем ни закрыть на GitHub, ни открыть новую — пропустить чистку
            await message.answer("GitHub не подключён. Открой /settings → GitHub.")
            return
        logger.info("[session] Session %s timed out for chat %s — auto-close + new", session_id, chat_id)
        try:
            await state.clear()
        except Exception:
            pass
        # Закрыть старую на GitHub (может вернуть False — orphan, лог-warning)
        if not await _set_session_status(session_id, "completed", *creds):
            logger.warning("[session] Failed to mark old session %s completed (orphan possible)", session_id)
        _t, _repo, _br = creds
        thread_url = f"https://github.com/{_repo}/blob/{_br}/{_SESSIONS_PATH}/{session_id}-thread.md"
        await message.answer(
            f"Прошлая сессия ({session_id}) закрыта по таймауту (30 мин без активности).\n"
            f"Thread: {thread_url}\n"
            "Начинаю новую с вашего сообщения."
        )
        token, repo, branch = creds
        await _open_new_session(message, state, text, token, repo, branch)
        return

    creds = await _get_github_creds(chat_id)
    if not creds:
        await message.answer("GitHub не подключён. Открой /settings → GitHub.")
        return

    token, repo, branch = creds
    turn_n = int(data.get("turn_count", 0)) + 1
    try:
        await state.update_data(turn_count=turn_n, last_turn_at=_now_iso_for_state())
    except Exception as exc:
        logger.error("[session] FSM update_data failed: %s", type(exc).__name__)
        await message.answer("Не удалось обновить состояние. Попробуйте ещё раз.")
        return

    ok = await _append_pilot_turn(session_id, message.message_id, text, turn_n, token, repo, branch)
    if ok:
        await message.answer("⏳ Работаю...")
    else:
        await message.answer("Ошибка при записи хода. Попробуй ещё раз.")


@external_session_router.message(Command("close"))
async def cmd_close(message: Message, state: FSMContext) -> None:
    """/close — complete active session."""
    chat_id = message.chat.id
    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None
    if current_state != ExternalSession.active.state:
        await message.answer("Нет активной сессии.")
        return

    data = await _get_session_data(state)
    session_id = (data or {}).get("session_id")
    try:
        await state.clear()
    except Exception as exc:
        logger.warning("[session] state.clear() failed in cmd_close: %s", type(exc).__name__)

    if not session_id:
        await message.answer("Сессия закрыта локально (state без session_id).")
        return

    creds = await _get_github_creds(chat_id)
    if creds:
        token, repo, branch = creds
        ok = await _set_session_status(session_id, "completed", token, repo, branch)
        if ok:
            thread_url = f"https://github.com/{repo}/blob/{branch}/{_SESSIONS_PATH}/{session_id}-thread.md"
            await message.answer(
                f"Сессия {session_id} завершена.\n"
                f"Thread: {thread_url}\n"
                "Финализация в sessions/external/ — Day Open покажет завтра."
            )
            return
    await message.answer("Сессия закрыта локально (GitHub недоступен).")


@external_session_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """/cancel — cancel active session."""
    chat_id = message.chat.id
    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None
    if current_state != ExternalSession.active.state:
        await message.answer("Нет активной сессии.")
        return

    data = await _get_session_data(state)
    session_id = (data or {}).get("session_id")
    try:
        await state.clear()
    except Exception as exc:
        logger.warning("[session] state.clear() failed in cmd_cancel: %s", type(exc).__name__)

    if not session_id:
        await message.answer("Сессия отменена локально (state без session_id).")
        return

    creds = await _get_github_creds(chat_id)
    if creds:
        ok = await _set_session_status(session_id, "cancelled", *creds)
        if ok:
            await message.answer(f"Сессия {session_id} отменена.")
            return
    await message.answer("Сессия отменена локально (GitHub недоступен).")
