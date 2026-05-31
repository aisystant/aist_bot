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
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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

# WP-358 Ф10.7: per-bot routing. BOT_FLAVOR в env (Railway):
# - pilot → @aist_pilot_bot — dispatcher шлёт через TG_BOT_TOKEN_PILOT
# - prod  → @aist_me_bot    — dispatcher шлёт через TG_BOT_TOKEN_PROD (= TG_BOT_TOKEN для bwd compat)
# Default "prod" если не задан (backwards compat: до Ф10.7 dispatcher всегда шёл в prod).
_BOT_FLAVOR = os.getenv("BOT_FLAVOR", "prod").strip().lower()

# WP-358 Ф10.5: финализация sessions → sessions/external/YYYY-MM/SESSION-<id>/
_SESSIONS_EXTERNAL_BASE = "sessions/external"
_SESSIONS_EXTERNAL_INDEX = "sessions/external/00-index.md"
_RECOVERY_INTER_TASK_DELAY_SEC = 1.0  # rate-limit guard между orphan'ами при startup recovery
_MAX_RECOVERY_PER_RUN = 10            # cap orphan'ов на один scan (защита от cascade-инцидента)
_RECOVERY_PERIODIC_INTERVAL_SEC = 3600 # periodic re-scan каждый час (защита от backlog при стабильном боте)
# Hotfix 2026-05-29: cutover-date — recovery игнорирует SESSION-* старше этой даты
# (pilot решил «backfill не нужен», но recovery scan на pre-cutover orphan'ы спамил).
# Формат SESSION-id: SESSION-YYYYMMDD-HHMMSS-XXXXXX → извлекаем YYYYMMDD.
_RECOVERY_CUTOVER_DATE = "20260529"  # YYYYMMDD, без дефисов для прямого сравнения с SESSION-id

# Module-level set для fire-and-forget tasks — защита от GC.
# Python <3.10 может собрать "orphaned" task до завершения. Strong-ref здесь
# держит task пока тот не закончится; done_callback discard выгребает обратно.
_BG_TASKS: set[asyncio.Task] = set()

# WP-7 TGSH (2026-05-30): heartbeat-poller per active chat_id.
# Бот subscribes to session.heartbeat events in learning.domain_event, edits «⏳ Работаю...»
# message with elapsed seconds and triggers chat_action(typing). Soft cut-off: heartbeat missing >15 мин → fail.
_HEARTBEAT_POLLERS: dict[int, asyncio.Task] = {}
_HEARTBEAT_POLL_INTERVAL_SEC = 10
_HEARTBEAT_TIMEOUT_SEC = 15 * 60  # 15 мин без heartbeat → soft cut-off (TGSH6)


def _spawn(coro) -> asyncio.Task:
    """asyncio.create_task с защитой от GC (Medium-1)."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


async def _start_heartbeat_poller(
    bot, chat_id: int, session_id: str, working_message_id: int, turn_n: int,
) -> None:
    """WP-7 TGSH5/6: poll learning.domain_event для session.heartbeat → edit "⏳ Работаю...".

    Стопится автоматически при session.turn_completed/turn_failed event ИЛИ при отсутствии heartbeat ≥15 мин (soft cut-off).
    Также может быть отменён через _stop_heartbeat_poller (cmd_close/cmd_cancel/finalize).
    """
    # Cancel previous poller if any (idempotent)
    await _stop_heartbeat_poller(chat_id)

    async def _loop():
        from db.connection import get_learning_pool
        from aiogram.exceptions import TelegramBadRequest

        last_heartbeat_at: Optional[datetime] = None
        started = datetime.now(timezone.utc)
        try:
            pool = await get_learning_pool()
        except Exception as exc:
            logger.warning("[heartbeat] learning pool unavailable for %s: %s", session_id, exc)
            return

        while True:
            await asyncio.sleep(_HEARTBEAT_POLL_INTERVAL_SEC)
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT event_type, occurred_at, payload
                        FROM domain_event
                        WHERE event_type IN (
                                'session.heartbeat',
                                'session.turn_completed',
                                'session.turn_failed'
                            )
                          AND payload->>'session_id' = $1
                          AND (payload->>'turn_n')::int = $2
                        ORDER BY occurred_at DESC
                        LIMIT 1
                        """,
                        session_id, turn_n,
                    )
            except Exception as exc:
                logger.warning("[heartbeat] poll error for %s: %s", session_id, type(exc).__name__)
                continue

            now = datetime.now(timezone.utc)

            if row is None:
                # No heartbeat yet — soft-timeout считаем от старта poller
                if (now - started).total_seconds() > _HEARTBEAT_TIMEOUT_SEC:
                    await _heartbeat_soft_fail(bot, chat_id, session_id, working_message_id,
                                                reason="no_heartbeat")
                    return
                continue

            ev_type = row["event_type"]
            occurred_at = row["occurred_at"]
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)

            if ev_type == "session.turn_completed":
                logger.info("[heartbeat] %s turn %d completed — poller stops", session_id, turn_n)
                return

            if ev_type == "session.turn_failed":
                reason = (row["payload"] or {}).get("reason", "unknown")
                await _heartbeat_soft_fail(bot, chat_id, session_id, working_message_id,
                                            reason=reason)
                return

            # session.heartbeat
            last_heartbeat_at = occurred_at
            payload = row["payload"] or {}
            elapsed = payload.get("elapsed_sec", int((now - started).total_seconds()))
            hint = payload.get("progress_hint", "")
            new_text = f"⏳ Работаю... {elapsed}с"
            if hint:
                new_text += f" — {hint}"
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=working_message_id, text=new_text,
                )
            except TelegramBadRequest as exc:
                # Message can be old enough that TG forbids edit, OR message identical (no-op).
                if "message is not modified" not in str(exc).lower():
                    logger.info("[heartbeat] edit failed for %s: %s", session_id, exc)
            except Exception as exc:
                logger.warning("[heartbeat] edit failed for %s: %s", session_id, type(exc).__name__)
            # Typing indicator — родной TG UX, мгновенный
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass

            # Soft cut-off: проверка stale heartbeat
            if last_heartbeat_at and (now - last_heartbeat_at).total_seconds() > _HEARTBEAT_TIMEOUT_SEC:
                await _heartbeat_soft_fail(bot, chat_id, session_id, working_message_id,
                                            reason="stale_heartbeat")
                return

    task = _spawn(_loop())
    _HEARTBEAT_POLLERS[chat_id] = task
    task.add_done_callback(lambda t, cid=chat_id: _HEARTBEAT_POLLERS.pop(cid, None))


async def _stop_heartbeat_poller(chat_id: int) -> None:
    """Отменить активный heartbeat-poller для chat_id (если есть)."""
    task = _HEARTBEAT_POLLERS.pop(chat_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


async def _heartbeat_soft_fail(
    bot, chat_id: int, session_id: str, working_message_id: int, reason: str,
) -> None:
    """WP-7 TGSH6: soft-timeout fail — edit «⏳ Работаю...» → «❌ Превышен таймаут».

    Reason patterns: 'no_heartbeat' / 'stale_heartbeat' / 'usage_limit' / 'timeout' / 'unknown'.
    """
    from aiogram.exceptions import TelegramBadRequest
    reason_text = {
        "no_heartbeat": "обработчик не начал отвечать в течение 15 мин",
        "stale_heartbeat": "обработчик замолчал (нет сигналов жизни ≥15 мин)",
        "usage_limit": "превышен лимит обращений к Claude API — повтори через несколько минут",
        "timeout": "превышен системный таймаут обработчика (30 мин)",
        "unknown": "обработчик завершился с ошибкой",
    }.get(reason, "обработчик завершился с ошибкой")

    text = (
        f"❌ Ход не завершён: {reason_text}.\n"
        f"Сессия {session_id} помечена failed.\n"
        f"Открой новую: /claude <запрос>"
    )
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=working_message_id, text=text)
    except TelegramBadRequest:
        # Fallback — send fresh message
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            logger.warning("[heartbeat] fail message send failed for %s: %s", session_id, exc)
    except Exception as exc:
        logger.warning("[heartbeat] fail edit failed for %s: %s", session_id, exc)


async def recover_active_heartbeat_pollers(bot) -> None:
    """WP-7 TGSH7: boot recovery — restart heartbeat-pollers for active FSM sessions.

    Symmetric to recover_orphan_finalizations (orphan SESSION-* в GitHub),
    но для FSM-active в Neon (свежий last_turn_at, есть working_message_id).
    """
    try:
        from db.connection import get_pool
        pool = await get_pool()
    except Exception as exc:
        logger.warning("[heartbeat] boot recovery pool error: %s", type(exc).__name__)
        return

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, data
                FROM fsm_states
                WHERE state = $1
                  AND data->>'session_id' IS NOT NULL
                  AND data->>'last_turn_at' > to_char(NOW() - INTERVAL '30 minutes', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
                  AND data->>'working_message_id' IS NOT NULL
                """,
                "ExternalSession:active",
            )
    except Exception as exc:
        logger.warning("[heartbeat] boot recovery query failed: %s", type(exc).__name__)
        return

    for row in rows:
        try:
            data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
            key = row["key"]
            # FSM key format aiogram: "bot:<bot_id>:user:<user_id>:chat:<chat_id>" or similar
            chat_id_str = key.split(":chat:")[-1].split(":")[0]
            chat_id = int(chat_id_str)
            session_id = data["session_id"]
            working_message_id = int(data["working_message_id"])
            turn_n = int(data.get("turn_count", 1))
            await _start_heartbeat_poller(bot, chat_id, session_id, working_message_id, turn_n)
            logger.info("[heartbeat] boot recovery resumed poller for %s (chat=%s)", session_id, chat_id)
        except Exception as exc:
            logger.warning("[heartbeat] boot recovery row skip: %s", type(exc).__name__)


async def cancel_bg_tasks(timeout: float = 5.0) -> int:
    """Graceful shutdown: cancel + await все висящие fire-and-forget tasks.

    Вызывается из bot.py при SIGTERM перед exit. Возвращает количество cancelled tasks.
    """
    if not _BG_TASKS:
        return 0
    pending = list(_BG_TASKS)
    for t in pending:
        if not t.done():
            t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("[session] cancel_bg_tasks: %d tasks did not drain in %ds", len(pending), timeout)
    return len(pending)


# WP-358 Ф10.5 iter 4: per-session lock (C-1/C-2 fix).
# Защита от concurrent _finalize_session(sid) от двух источников
# (periodic recovery + cmd_finalize, periodic + auto-close timeout, etc.) →
# race в step 7 (index append) silently теряла бы строку в 00-index.md.
_FINALIZE_LOCKS: dict[str, asyncio.Lock] = {}
_FINALIZE_LOCKS_MUTEX = asyncio.Lock()


async def _get_finalize_lock(sid: str) -> asyncio.Lock:
    """Возвращает (создавая если нужно) lock для конкретного session_id."""
    async with _FINALIZE_LOCKS_MUTEX:
        lock = _FINALIZE_LOCKS.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            _FINALIZE_LOCKS[sid] = lock
        return lock

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
    """Returns (decoded_content, sha) or (None, None) on miss/error.

    WP-358 Ф10.5: при 403/429 (rate limit) один retry с retry-after.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    async def _attempt() -> tuple[int, Optional[dict], Optional[int]]:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=_gh_headers(token), params={"ref": branch}) as resp:
                retry_after = int(resp.headers.get("retry-after", "0") or "0") or None
                if resp.status == 200:
                    return resp.status, await resp.json(), None
                return resp.status, None, retry_after

    status, data, retry_after = await _attempt()
    if status in (403, 429):
        wait = retry_after or 30
        logger.warning("[session] GET %s → %d, retry after %ds", path, status, wait)
        await asyncio.sleep(wait)
        status, data, _ = await _attempt()
    if status == 200 and data is not None:
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    if status == 404:
        return None, None
    if status == 401:
        logger.warning("[session] GitHub 401 for path=%s — token expired?", path)
        return None, None
    logger.error("[session] GET %s → %d", path, status)
    return None, None


async def _gh_put_file(
    path: str, content: str, msg: str, token: str, repo: str, branch: str,
    sha: Optional[str] = None,
) -> bool:
    """Create (sha=None) or update (sha=existing) file. Retries once on 422 (sha conflict)."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    async def _attempt(current_sha: Optional[str]) -> tuple[int, Optional[int]]:
        body: dict = {
            "message": msg,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if current_sha:
            body["sha"] = current_sha
        async with aiohttp.ClientSession() as sess:
            async with sess.put(url, headers=_gh_headers(token), json=body) as resp:
                retry_after = int(resp.headers.get("retry-after", "0") or "0") or None
                return resp.status, retry_after

    status, retry_after = await _attempt(sha)
    if status in (200, 201):
        return True

    # WP-358 Ф10.5: 403/429 rate-limit → один retry с retry-after из header
    if status in (403, 429):
        wait = retry_after or 30
        logger.warning("[session] PUT %s → %d rate-limit, retry after %ds", path, status, wait)
        await asyncio.sleep(wait)
        status, _ = await _attempt(sha)
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
            status, retry_after_2 = await _attempt(fresh_sha)
            if status in (200, 201):
                return True
            # Если после 422-retry попали в rate-limit — ещё одна попытка с retry-after
            if status in (403, 429):
                wait = retry_after_2 or 30
                logger.warning("[session] PUT %s → %d after 422-retry, sleep %ds", path, status, wait)
                await asyncio.sleep(wait)
                status, _ = await _attempt(fresh_sha)
                if status in (200, 201):
                    return True

    if status == 401:
        logger.warning("[session] PUT %s → 401 token expired", path)
    elif status == 403:
        logger.warning("[session] PUT %s → 403 (rate-limit or permissions)", path)
    else:
        logger.error("[session] PUT %s → %d", path, status)
    return False

# ── WP-358 Ф10.5: helpers для финализации ─────────────────────────────────────

async def _gh_list_dir(
    path: str, token: str, repo: str, branch: str
) -> list[dict]:
    """Returns list of dir entries or [] on miss/error. Each entry: {name, type, sha, path}."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, headers=_gh_headers(token), params={"ref": branch}) as resp:
            if resp.status == 404:
                return []
            if resp.status != 200:
                logger.warning("[session] LIST %s → %d", path, resp.status)
                return []
            data = await resp.json()
            return data if isinstance(data, list) else []


async def _gh_delete_file(
    path: str, sha: str, msg: str, token: str, repo: str, branch: str
) -> bool:
    """DELETE file. Returns True if 200/404 (idempotent), False on other error.

    Retry on:
    - 403/429 rate-limit (с retry-after header)
    - 422 sha conflict (re-fetch sha + повторить)
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    async def _attempt(current_sha: str) -> tuple[int, Optional[int]]:
        body = {"message": msg, "sha": current_sha, "branch": branch}
        async with aiohttp.ClientSession() as sess:
            async with sess.delete(url, headers=_gh_headers(token), json=body) as resp:
                retry_after = int(resp.headers.get("retry-after", "0") or "0") or None
                return resp.status, retry_after

    status, retry_after = await _attempt(sha)
    if status in (200, 404):
        return True

    # Rate-limit retry с retry-after
    if status in (403, 429):
        wait = retry_after or 30
        logger.warning("[session] DELETE %s → %d rate-limit, retry after %ds", path, status, wait)
        await asyncio.sleep(wait)
        status, _ = await _attempt(sha)
        if status in (200, 404):
            return True

    # 422 sha conflict — re-fetch и повторить (симметрично _gh_put_file)
    if status == 422:
        logger.warning("[session] DELETE %s → 422 SHA conflict, retrying", path)
        _, fresh_sha = await _gh_get_file(path, token, repo, branch)
        if fresh_sha is None:
            # Файл уже удалён конкурентным процессом — idempotent success
            return True
        status, retry_after_2 = await _attempt(fresh_sha)
        if status in (200, 404):
            return True
        # Symmetric с _gh_put_file: если 422-retry попал в rate-limit — ещё попытка
        if status in (403, 429):
            wait = retry_after_2 or 30
            logger.warning("[session] DELETE %s → %d after 422-retry, sleep %ds", path, status, wait)
            await asyncio.sleep(wait)
            status, _ = await _attempt(fresh_sha)
            if status in (200, 404):
                return True

    logger.error("[session] DELETE %s → %d", path, status)
    return False


# Базовая транслитерация cyrillic → latin (минимум без внешних зависимостей).
# Через dict (поддерживает пустые значения для ъ/ь, не зависит от длин).
_TRANSLIT_MAP = {ord(k): v for k, v in {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "c", "ш": "s", "щ": "s",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "u", "я": "y",
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E",
    "Ж": "J", "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M",
    "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "H", "Ц": "C", "Ч": "C", "Ш": "S", "Щ": "S",
    "Ъ": "", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "U", "Я": "Y",
}.items()}


def _slugify_topic(thread_text: str) -> Optional[str]:
    """Выделить тему из thread (первая значимая строка после turn:1 pilot).

    Returns slug ≤60 символов (lowercase, kebab-case) или None если ничего нет.
    Не используется в URL/path (см. WP-358 Ф10.5 ArchGate), только в metadata.
    """
    if not thread_text:
        return None
    lines = thread_text.split("\n")
    capture = False
    candidate = ""
    for line in lines:
        if line.startswith("[turn:1, role:pilot"):
            capture = True
            continue
        if capture:
            if line.startswith("[turn:"):
                break
            s = line.strip()
            if s:
                candidate = s
                break
    if not candidate:
        return None
    # Транслитерация cyrillic→latin
    s = candidate.translate(_TRANSLIT_MAP).lower()
    # Заменить всё non-alphanumeric на "-", сжать дубли, обрезать
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        return None
    return s[:60].rstrip("-") or None


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
    """Сгенерировать meta SESSION-файла.

    WP-358 Ф10.7: target_bot указывает dispatcher'у через какой токен слать ответ.
    """
    return (
        f"---\n"
        f"session_id: {session_id}\n"
        f"tg_chat_id: {tg_chat_id}\n"
        f"target_bot: {_BOT_FLAVOR}\n"
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
    # P3 fix: anchor regex via ^...$ + MULTILINE — защита от случайных захватов
    # чужих ключей при росте meta-схемы (last_status:, status_history: и др.).
    new_meta = re.sub(r"^status:.*$", f"status: {status}", cur_meta, flags=re.MULTILINE)
    return await _gh_put_file(
        meta_path, new_meta,
        f"session: {status} {session_id}",
        token, repo, branch, meta_sha,
    )

# ── WP-358 Ф10.5: финализация SESSION-* → sessions/external/YYYY-MM/SESSION-<id>/ ──

def _build_report_md(
    session_id: str,
    meta_text: str,
    thread_text: str,
    topic_slug: Optional[str],
) -> str:
    """Сгенерировать report.md для финализированной сессии. outcome=null (Ф10.5)."""
    # Парсинг turn_count и created_at из meta
    turns_m = re.search(r"^turn_count:\s*\"?([0-9]+)\"?", meta_text, re.M)
    created_m = re.search(r"^created_at:\s*\"?([^\"\n]+)\"?", meta_text, re.M)
    turns = turns_m.group(1) if turns_m else "?"
    created = created_m.group(1) if created_m else ""
    finalized = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date = created[:10] if created else finalized[:10]

    fm_topic = f'topic: "{topic_slug}"' if topic_slug else "topic: null"
    return (
        "---\n"
        f"session_id: {session_id}\n"
        f"date: {date}\n"
        f"{fm_topic}\n"
        f"outcome: null\n"
        f"turns: {turns}\n"
        f"finalized_at: {finalized}\n"
        "---\n\n"
        f"# Финализированная сессия {session_id}\n\n"
        f"**Тема:** {topic_slug or '(не определена)'}\n\n"
        f"**Ходов:** {turns} · **Открыта:** {created} · **Закрыта:** {finalized}\n\n"
        "## Диалог\n\n"
        f"См. [thread.md](thread.md) — полная стенограмма.\n\n"
        "## Итог\n\n"
        "_(заполнить вручную: главное решение / consensus / открытые вопросы)_\n"
    )


async def _finalize_session(
    session_id: str,
    chat_id: int,
    token: str,
    repo: str,
    branch: str,
) -> tuple[bool, Optional[str]]:
    """Перенести SESSION-* из inbox/agent/sessions/ в sessions/external/YYYY-MM/SESSION-<id>/.

    Идемпотентно: проверяет файловое состояние перед каждым шагом, skip если уже сделано.
    Per-sid lock (C-1/C-2): два concurrent вызова для одного sid сериализуются →
    второй увидит уже сделанную работу (idempotent skip) → нет race в index append.
    Returns (success, report_url_or_error_message).
    """
    lock = await _get_finalize_lock(session_id)
    async with lock:
        return await _finalize_session_impl(session_id, chat_id, token, repo, branch)


async def _finalize_session_impl(
    session_id: str,
    chat_id: int,
    token: str,
    repo: str,
    branch: str,
) -> tuple[bool, Optional[str]]:
    """Тело финализации (защищено per-sid lock'ом, не вызывать напрямую)."""
    src_meta_path = f"{_SESSIONS_PATH}/{session_id}.md"
    src_thread_path = f"{_SESSIONS_PATH}/{session_id}-thread.md"

    # 1) GET meta + thread параллельно (1 round-trip эффективно)
    results = await asyncio.gather(
        _gh_get_file(src_meta_path, token, repo, branch),
        _gh_get_file(src_thread_path, token, repo, branch),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            return False, f"GET failed: {type(r).__name__}"
    (meta_text, meta_sha), (thread_text, thread_sha) = results

    if meta_text is None and thread_text is None:
        # Уже всё удалено — финализация прошла ранее. Проверяем что папка существует.
        return True, None
    if meta_text is None:
        # Partial state: meta удалён, thread остался. Не можем восстановить полный
        # frontmatter (turns/created_at). Reject — пилот разберётся вручную.
        logger.error("[session] finalize %s: meta missing, thread present (corrupt state)", session_id)
        return False, "meta missing — manual cleanup required"

    # 2) Определить month по created_at в meta (если есть)
    created_m = re.search(r"^created_at:\s*\"?([0-9]{4}-[0-9]{2})", meta_text or "", re.M)
    if created_m:
        month_dir = created_m.group(1)
    else:
        # fallback: из session_id YYYYMMDD
        m = re.search(r"SESSION-([0-9]{4})([0-9]{2})", session_id)
        month_dir = f"{m.group(1)}-{m.group(2)}" if m else datetime.now(timezone.utc).strftime("%Y-%m")

    dst_dir = f"{_SESSIONS_EXTERNAL_BASE}/{month_dir}/{session_id}"
    dst_report = f"{dst_dir}/report.md"
    dst_thread = f"{dst_dir}/thread.md"

    topic_slug = _slugify_topic(thread_text or "")

    # 3) PUT report.md (idempotent — skip если есть)
    existing_report, _ = await _gh_get_file(dst_report, token, repo, branch)
    if existing_report is None:
        report_content = _build_report_md(session_id, meta_text or "", thread_text or "", topic_slug)
        ok = await _gh_put_file(
            dst_report, report_content,
            f"finalize {session_id}: report.md",
            token, repo, branch,
        )
        if not ok:
            return False, "PUT report.md failed"

    # 4) PUT thread.md (копия)
    existing_thread, _ = await _gh_get_file(dst_thread, token, repo, branch)
    if existing_thread is None and thread_text is not None:
        ok = await _gh_put_file(
            dst_thread, thread_text,
            f"finalize {session_id}: thread.md",
            token, repo, branch,
        )
        if not ok:
            return False, "PUT thread.md failed"

    # 5) DELETE source meta (idempotent + re-fetch sha против race с _set_session_status)
    if meta_sha is not None:
        _, fresh_meta_sha = await _gh_get_file(src_meta_path, token, repo, branch)
        if fresh_meta_sha is not None:
            ok = await _gh_delete_file(
                src_meta_path, fresh_meta_sha,
                f"finalize {session_id}: remove source meta",
                token, repo, branch,
            )
            if not ok:
                return False, "DELETE source meta failed"

    # 6) DELETE source thread (idempotent + re-fetch sha)
    if thread_sha is not None:
        _, fresh_thread_sha = await _gh_get_file(src_thread_path, token, repo, branch)
        if fresh_thread_sha is not None:
            ok = await _gh_delete_file(
                src_thread_path, fresh_thread_sha,
                f"finalize {session_id}: remove source thread",
                token, repo, branch,
            )
            if not ok:
                return False, "DELETE source thread failed"

    # 7) Append in sessions/external/00-index.md (idempotent через grep)
    idx_text, idx_sha = await _gh_get_file(_SESSIONS_EXTERNAL_INDEX, token, repo, branch)
    finalized = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_row = f"| {finalized[:10]} | {session_id} | {topic_slug or '(no topic)'} | {dst_report} |\n"
    if idx_text is None:
        idx_content = (
            "# External Sessions Index\n\n"
            "Финализированные сессии Telegram-канала /claude (WP-358 Ф10.5).\n\n"
            "| Дата | Session ID | Тема | Report |\n"
            "|------|-----------|------|--------|\n"
            + new_row
        )
        await _gh_put_file(
            _SESSIONS_EXTERNAL_INDEX, idx_content,
            f"finalize {session_id}: init index",
            token, repo, branch,
        )
    elif session_id not in idx_text:
        idx_content = idx_text.rstrip() + "\n" + new_row
        await _gh_put_file(
            _SESSIONS_EXTERNAL_INDEX, idx_content,
            f"finalize {session_id}: append index",
            token, repo, branch, idx_sha,
        )

    report_url = f"https://github.com/{repo}/blob/{branch}/{dst_report}"
    return True, report_url


async def _safe_send(bot, chat_id: int, text: str, reply_markup=None) -> None:
    """TG send_message с защитой от лост-таска (Critical-2)."""
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception:
        logger.exception("[session] TG send failed for chat %s", chat_id)


def _outcome_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """WP-358 Ф10.6: inline-keyboard для оценки outcome финализированной сессии.

    Кнопки кодируют outcome в callback_data: outcome:<consensus|partial|abandoned|utility>:<sid>.
    Пилот может проигнорировать — outcome остаётся null (default по DP.SC.NNN §close).
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Решено", callback_data=f"outcome:consensus:{session_id}"),
            InlineKeyboardButton(text="🚧 Частично", callback_data=f"outcome:partial:{session_id}"),
        ],
        [
            InlineKeyboardButton(text="❌ Отменено", callback_data=f"outcome:abandoned:{session_id}"),
            InlineKeyboardButton(text="🤷 Утилитарная", callback_data=f"outcome:utility:{session_id}"),
        ],
    ])


def _finalize_done_callback(
    task: asyncio.Task,
    chat_id: int,
    session_id: str,
    bot,
) -> None:
    """Done callback для fire-and-forget _finalize_session. Шлёт TG-уведомление по исходу."""
    try:
        success, payload = task.result()
    except Exception as exc:
        logger.exception("[session] Finalization task raised for %s", session_id)
        _spawn(_safe_send(
            bot, chat_id,
            f"⚠️ Финализация {session_id} не удалась: {type(exc).__name__}\n"
            f"Повторить вручную: /finalize {session_id}"
        ))
        return
    if success and payload:
        # WP-358 Ф10.6: inline-keyboard для outcome (опционально, пилот может проигнорировать)
        _spawn(_safe_send(
            bot, chat_id,
            f"✅ Финализация {session_id} готова.\nОтчёт: {payload}\n\nКак прошла сессия?",
            reply_markup=_outcome_keyboard(session_id),
        ))
    elif success:
        # уже была финализирована (no-op idempotent)
        logger.info("[session] Finalization no-op for %s (already done)", session_id)
    else:
        _spawn(_safe_send(
            bot, chat_id,
            f"⚠️ Финализация {session_id} не удалась: {payload}\n"
            f"Повторить вручную: /finalize {session_id}"
        ))


async def recover_orphan_finalizations(bot) -> None:
    """Startup recovery: найти SESSION-* со status: completed в inbox без папки в sessions/external/.

    Использует pilot-credentials (_PILOT_PAT, _PILOT_REPO) и chat_id из meta для каждого orphan'а.
    Sequential обработка с задержкой между orphan'ами (rate-limit safe).
    """
    if not _PILOT_PAT or not _PILOT_REPO:
        logger.info("[session] recovery: no pilot creds, skip")
        return
    repo = _normalize_repo(_PILOT_REPO)
    token = _PILOT_PAT
    branch = "main"

    inbox_entries = await _gh_list_dir(_SESSIONS_PATH, token, repo, branch)
    # Сборка completed-сессий из inbox (без -thread.md, без pre-cutover)
    completed_sessions: list[str] = []
    skipped_pre_cutover = 0
    for entry in inbox_entries:
        name = entry.get("name", "")
        if entry.get("type") != "file" or not name.startswith("SESSION-") or name.endswith("-thread.md"):
            continue
        sid = name[:-3]  # strip .md
        # Cutover-фильтр: SESSION-YYYYMMDD-... — игнорируем pre-cutover
        m = re.match(r"^SESSION-([0-9]{8})-", sid)
        if not m or m.group(1) < _RECOVERY_CUTOVER_DATE:
            skipped_pre_cutover += 1
            continue
        # Прочитать meta — проверить status: completed
        path = entry.get("path") or f"{_SESSIONS_PATH}/{name}"
        meta_text, _ = await _gh_get_file(path, token, repo, branch)
        if meta_text and re.search(r'^status:\s*"?completed"?', meta_text, re.M):
            completed_sessions.append(sid)
    if skipped_pre_cutover > 0:
        logger.info("[session] recovery: skipped %d pre-cutover orphans (date < %s)",
                    skipped_pre_cutover, _RECOVERY_CUTOVER_DATE)

    if not completed_sessions:
        logger.info("[session] recovery: no completed orphans in inbox")
        return

    if len(completed_sessions) > _MAX_RECOVERY_PER_RUN:
        logger.warning(
            "[session] recovery: %d orphans, capping to %d (defer rest to next restart)",
            len(completed_sessions), _MAX_RECOVERY_PER_RUN
        )
        completed_sessions = completed_sessions[:_MAX_RECOVERY_PER_RUN]
    else:
        logger.info("[session] recovery: %d completed orphan candidates", len(completed_sessions))

    for sid in completed_sessions:
        # Извлечь month и chat_id из meta — нужны для финализации
        meta_path = f"{_SESSIONS_PATH}/{sid}.md"
        meta_text, _ = await _gh_get_file(meta_path, token, repo, branch)
        if not meta_text:
            continue
        chat_m = re.search(r"^tg_chat_id:\s*([0-9]+)", meta_text, re.M)
        if not chat_m:
            logger.warning("[session] recovery: SESSION %s meta lacks tg_chat_id, skip", sid)
            continue
        chat_id = int(chat_m.group(1))
        # Если papка уже есть — пропустить (предотвратить retry storm)
        created_m = re.search(r"^created_at:\s*\"?([0-9]{4}-[0-9]{2})", meta_text, re.M)
        month_dir = created_m.group(1) if created_m else None
        if month_dir:
            dst_report_path = f"{_SESSIONS_EXTERNAL_BASE}/{month_dir}/{sid}/report.md"
            existing, _ = await _gh_get_file(dst_report_path, token, repo, branch)
            if existing is not None:
                # Папка есть, но source ещё в inbox — нужно доделать DELETE
                logger.info("[session] recovery: %s has report, completing finalize", sid)
        # Запустить финализацию (fire-and-forget с callback)
        task = _spawn(_finalize_session(sid, chat_id, token, repo, branch))
        task.add_done_callback(
            lambda t, sid_=sid, cid=chat_id, b=bot: _finalize_done_callback(t, cid, sid_, b)
        )
        await asyncio.sleep(_RECOVERY_INTER_TASK_DELAY_SEC)


async def _periodic_recovery_loop(bot, interval_sec: int) -> None:
    """High-3: периодический re-scan каждые N секунд (защита от backlog при стабильном боте).

    Запускается отдельной long-running task на старте. Защищает от случая:
    >10 orphan'ов накапливаются за раз → cap режет до 10 → стабильный бот → backlog растёт.
    Exponential backoff при N consecutive failures (защита от log spam при GitHub outage).
    """
    failure_count = 0
    while True:
        try:
            # Exponential backoff: interval × 2^min(failures, 4), max 16× = 16 часов
            multiplier = 2 ** min(failure_count, 4)
            await asyncio.sleep(interval_sec * multiplier)
            await recover_orphan_finalizations(bot)
            failure_count = 0  # reset на успехе
        except asyncio.CancelledError:
            raise
        except Exception:
            failure_count += 1
            logger.exception(
                "[session] periodic recovery iteration failed (consecutive: %d, next sleep ×%d)",
                failure_count, 2 ** min(failure_count, 4),
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
    await message.answer(
        f"Сессия открыта: {session_id}\n"
        "Claude обработает запрос и ответит здесь."
    )
    # WP-7 TGSH5: захватываем message_id «⏳ Работаю...» для heartbeat-edit.
    working_msg = await message.answer("⏳ Работаю...")
    try:
        await state.set_state(ExternalSession.active)
        await state.update_data(
            session_id=session_id,
            turn_count=1,
            last_turn_at=_now_iso_for_state(),
            working_message_id=working_msg.message_id,
        )
    except Exception as exc:
        logger.error("[session] FSM set_state failed for %s: %s", chat_id, type(exc).__name__)
        await message.answer(
            f"Сессия создана ({session_id}), но не удалось сохранить state. "
            "Возможно проблемы с БД — попробуйте через минуту."
        )
        return
    await _start_heartbeat_poller(message.bot, chat_id, session_id, working_msg.message_id, turn_n=1)


@external_session_router.message(Command("claude", ignore_case=True))
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
            ok = await _append_pilot_turn(session_id, message.message_id, user_text, turn_n, token, repo, branch)
            if ok:
                # WP-7 TGSH5: захват working_message_id для heartbeat-edit + старт poller'а.
                working_msg = await message.answer("⏳ Работаю...")
                try:
                    await state.update_data(
                        turn_count=turn_n,
                        last_turn_at=_now_iso_for_state(),
                        working_message_id=working_msg.message_id,
                    )
                except Exception as exc:
                    logger.error("[session] FSM update_data failed: %s", type(exc).__name__)
                    await message.answer("Не удалось обновить состояние. Попробуйте ещё раз.")
                    return
                await _start_heartbeat_poller(message.bot, chat_id, session_id, working_msg.message_id, turn_n)
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
        token, repo, branch = creds
        # WP-358 Ф10.5: fire-and-forget финализация старой сессии
        await message.answer(
            f"Прошлая сессия ({session_id}) закрыта по таймауту (30 мин без активности).\n"
            f"Финализация запущена. Начинаю новую с вашего сообщения."
        )
        task = _spawn(_finalize_session(session_id, chat_id, token, repo, branch))
        task.add_done_callback(
            lambda t, sid=session_id, cid=chat_id, b=message.bot: _finalize_done_callback(t, cid, sid, b)
        )
        await _open_new_session(message, state, text, token, repo, branch)
        return

    creds = await _get_github_creds(chat_id)
    if not creds:
        await message.answer("GitHub не подключён. Открой /settings → GitHub.")
        return

    token, repo, branch = creds
    turn_n = int(data.get("turn_count", 0)) + 1

    ok = await _append_pilot_turn(session_id, message.message_id, text, turn_n, token, repo, branch)
    if ok:
        # WP-7 TGSH5: захват working_message_id + старт heartbeat-poller'а.
        working_msg = await message.answer("⏳ Работаю...")
        try:
            await state.update_data(
                turn_count=turn_n,
                last_turn_at=_now_iso_for_state(),
                working_message_id=working_msg.message_id,
            )
        except Exception as exc:
            logger.error("[session] FSM update_data failed: %s", type(exc).__name__)
            await message.answer("Не удалось обновить состояние. Попробуйте ещё раз.")
            return
        await _start_heartbeat_poller(message.bot, chat_id, session_id, working_msg.message_id, turn_n)
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
    # WP-7 TGSH5: остановить heartbeat-poller перед очисткой state (cmd_close).
    await _stop_heartbeat_poller(chat_id)
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
        # H-1 fix: не blocking-retry внутри handler — сразу ack + fire-and-forget весь
        # set_status + retry + finalize в отдельном task (_close_and_finalize).
        await message.answer(f"Сессия {session_id} закрывается. Отчёт придёт когда готов.")
        _spawn(_close_and_finalize(session_id, chat_id, token, repo, branch, message.bot))
        return
    await message.answer("Сессия закрыта локально (GitHub недоступен).")


def _expected_report_path(session_id: str) -> str:
    """Ожидаемый путь к report.md для уже финализированной SESSION-<id>."""
    m = re.match(r"^SESSION-([0-9]{4})([0-9]{2})", session_id)
    month_dir = f"{m.group(1)}-{m.group(2)}" if m else datetime.now(timezone.utc).strftime("%Y-%m")
    return f"{_SESSIONS_EXTERNAL_BASE}/{month_dir}/{session_id}/report.md"


async def _check_already_finalized(
    session_id: str, token: str, repo: str, branch: str,
) -> Optional[str]:
    """Если sessions/external/.../report.md уже существует → вернуть GitHub URL."""
    report_path = _expected_report_path(session_id)
    existing, _ = await _gh_get_file(report_path, token, repo, branch)
    if existing is not None:
        return f"https://github.com/{repo}/blob/{branch}/{report_path}"
    return None


async def _close_and_finalize(
    session_id: str, chat_id: int, token: str, repo: str, branch: str, bot,
) -> None:
    """H-1 fix: set_status(completed) + retry + finalize, всё в отдельном task.

    Не блокирует cmd_close handler — пилот получает ack мгновенно.
    Hotfix 29 мая: при set_status fail — проверить, не финализирована ли уже сессия
    (например, periodic recovery её схватил). Если да — ack ссылкой, а не false-alarm.
    """
    ok = await _set_session_status(session_id, "completed", token, repo, branch)
    if not ok:
        # Возможно уже финализирована (source файл удалён). Проверить sessions/external/.
        report_url = await _check_already_finalized(session_id, token, repo, branch)
        if report_url:
            await _safe_send(
                bot, chat_id,
                f"ℹ️ Сессия {session_id} уже была закрыта ранее.\nОтчёт: {report_url}"
            )
            return
        logger.warning("[session] _close_and_finalize: set_status fail for %s, retry", session_id)
        await asyncio.sleep(5)
        ok = await _set_session_status(session_id, "completed", token, repo, branch)
    if not ok:
        # Второй retry тоже fail. Повторная проверка финализации
        # (между первой проверкой и retry могла пройти).
        report_url = await _check_already_finalized(session_id, token, repo, branch)
        if report_url:
            await _safe_send(
                bot, chat_id,
                f"ℹ️ Сессия {session_id} уже была закрыта ранее.\nОтчёт: {report_url}"
            )
            return
        await _safe_send(
            bot, chat_id,
            f"⚠️ Не удалось пометить {session_id} как completed (GitHub error).\n"
            f"Сессия закрыта локально. Повторить позже: /finalize {session_id}"
        )
        return
    # Финализация — сразу в этом же task (lock внутри _finalize_session защищает от race)
    try:
        success, payload = await _finalize_session(session_id, chat_id, token, repo, branch)
    except Exception as exc:
        logger.exception("[session] _close_and_finalize: finalize raised for %s", session_id)
        await _safe_send(
            bot, chat_id,
            f"⚠️ Финализация {session_id} не удалась: {type(exc).__name__}\n"
            f"Повторить вручную: /finalize {session_id}"
        )
        return
    if success and payload:
        await _safe_send(bot, chat_id, f"✅ Финализация {session_id} готова.\nОтчёт: {payload}")
    elif not success:
        await _safe_send(
            bot, chat_id,
            f"⚠️ Финализация {session_id} не удалась: {payload}\n"
            f"Повторить вручную: /finalize {session_id}"
        )


# WP-358 Ф10.5 Medium fix: cmd_finalize spam-guard — отслеживание in-flight finalize
# на уровне session_id (защита от спама /finalize SESSION-id подряд).
_FINALIZE_INFLIGHT: set[str] = set()


@external_session_router.message(Command("finalize"))
async def cmd_finalize(message: Message, command: CommandObject) -> None:
    """WP-358 Ф10.5: /finalize <SESSION-id> — повторная финализация после fail."""
    if not command.args:
        await message.answer("Использование: /finalize SESSION-YYYYMMDD-HHMMSS-XXXXXX")
        return
    session_id = command.args.strip()
    if not re.match(r"^SESSION-[0-9]{8}-[0-9]{6}-[A-Za-z0-9]+$", session_id):
        await message.answer("Неверный формат session_id. Ожидается SESSION-YYYYMMDD-HHMMSS-XXXXXX.")
        return

    # Medium fix: spam-guard — если /finalize той же сессии уже в работе, отказать
    if session_id in _FINALIZE_INFLIGHT:
        await message.answer(f"Финализация {session_id} уже в работе. Подожди.")
        return

    chat_id = message.chat.id
    creds = await _get_github_creds(chat_id)
    if not creds:
        await message.answer("GitHub не подключён. Открой /settings → GitHub.")
        return
    token, repo, branch = creds

    # Ownership check: tg_chat_id из meta == sender chat_id (hard guard, Critical-3)
    meta_path = f"{_SESSIONS_PATH}/{session_id}.md"
    meta_text, _ = await _gh_get_file(meta_path, token, repo, branch)
    if meta_text is None:
        # meta удалён — может быть финализация прошла. Не даём доступ к чужой папке.
        await message.answer("Не нашёл meta сессии — финализация невозможна.")
        return
    chat_m = re.search(r"^tg_chat_id:\s*([0-9]+)", meta_text, re.M)
    if chat_m and int(chat_m.group(1)) != chat_id:
        await message.answer("Это не ваша сессия.")
        return
    if not chat_m:
        # corrupted meta без tg_chat_id — не доверяем
        await message.answer("В meta сессии нет tg_chat_id — финализация заблокирована.")
        return

    await message.answer(f"Повторная финализация {session_id}...")
    _FINALIZE_INFLIGHT.add(session_id)
    task = _spawn(_finalize_session(session_id, chat_id, token, repo, branch))
    task.add_done_callback(lambda t, sid=session_id: _FINALIZE_INFLIGHT.discard(sid))
    task.add_done_callback(
        lambda t, sid=session_id, cid=chat_id, b=message.bot: _finalize_done_callback(t, cid, sid, b)
    )


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
    # WP-7 TGSH5: остановить heartbeat-poller перед очисткой state (cmd_cancel).
    await _stop_heartbeat_poller(chat_id)
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


# ── WP-358 Ф10.6: outcome callback handler ────────────────────────────────────

@external_session_router.callback_query(F.data.startswith("outcome:"))
async def on_outcome_callback(cb: CallbackQuery) -> None:
    """Записать outcome в report.md финализированной сессии (Ф10.6).

    callback_data формат: outcome:<consensus|partial|abandoned|utility>:<session_id>.
    Ownership check: только владелец сессии (tg_chat_id в meta пред-финализации).
    Поскольку meta уже удалён, проверяем через chat_id callback'а ↔ chat_id меню
    (kept in original send_message — TG гарантирует доставку только в исходный чат).
    """
    parts = (cb.data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "outcome":
        await cb.answer("Неверный формат", show_alert=False)
        return
    outcome, session_id = parts[1], parts[2]
    if outcome not in ("consensus", "partial", "abandoned", "utility"):
        await cb.answer("Неверный outcome", show_alert=False)
        return
    if not re.match(r"^SESSION-[0-9]{8}-[0-9]{6}-[A-Za-z0-9]+$", session_id):
        await cb.answer("Неверный session_id", show_alert=False)
        return

    chat_id = cb.message.chat.id if cb.message else None
    if chat_id is None:
        await cb.answer("Нет chat_id", show_alert=False)
        return

    creds = await _get_github_creds(chat_id)
    if not creds:
        await cb.answer("GitHub не подключён", show_alert=False)
        return
    token, repo, branch = creds

    report_path = _expected_report_path(session_id)
    cur, sha = await _gh_get_file(report_path, token, repo, branch)
    if cur is None:
        await cb.answer("Отчёт не найден", show_alert=True)
        return

    # Обновить outcome в frontmatter (anchored regex)
    new_text = re.sub(r"^outcome:.*$", f"outcome: {outcome}", cur, flags=re.MULTILINE)
    if new_text == cur:
        # outcome строки нет — добавить перед закрывающим ---
        new_text = re.sub(r"^---\s*$", f"outcome: {outcome}\n---", cur, count=1, flags=re.MULTILINE)
    ok = await _gh_put_file(
        report_path, new_text,
        f"session({session_id}): outcome={outcome}",
        token, repo, branch, sha,
    )
    if ok:
        # Убрать клавиатуру + ack
        try:
            if cb.message:
                await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer(f"Записано: {outcome}", show_alert=False)
    else:
        await cb.answer("Не удалось записать (GitHub error)", show_alert=True)
