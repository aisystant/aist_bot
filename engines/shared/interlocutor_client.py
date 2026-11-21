"""Клиент разговорного агента платформы (iwe-interlocutor, РП-262 Ф4).

Бот остаётся тонким окном: при включённом флаге он отправляет вопрос агенту вместе с пропуском
самого человека и показывает готовый ответ. Права проверяет шлюз, роль и инструменты определяет
агент. Любой сбой агента возвращает None, и бот отвечает прежним путём (запасной до этапа 6).

Флаги (без перевыкладки кода, откат тем же способом):
  INTERLOCUTOR_URL         - адрес POST /v1/turn; пусто = выключено для всех.
  INTERLOCUTOR_THREAD_MODE - "1" = выпуск А (WP-262 Ф10): нить хранит агент, бот не шлёт историю и при
                             сбое агента сам не отвечает; иначе прежнее поведение.
  Кому отвечает агент, решает вызывающий: всем, кто прошёл проверку подписки (WP-262 F4, 20.11).

В журнал пишутся только длительность и код исхода: вопросы, ответы и пропуск не пишутся.
"""

import asyncio
import logging
import os
import time
import weakref
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

BUDGET_S = 55.0
MAX_HISTORY_PAIRS = 5
MAX_TEXT_CHARS = 4000
FORGET_TIMEOUT_S = 10.0
RETRY_PAUSE_S = 1.0

# Result classes of one turn (WP-262 F10). The class decides what the window does next.
KIND_ANSWER = "answer"              # agent answered: show it
KIND_AGENT_MESSAGE = "agent_message"  # agent text that must not be replaced (fallback_allowed=false)
KIND_NO_PASS = "no_pass"            # no pass or the agent asks to log in
KIND_RETRIABLE = "retriable"        # transport failure, 429/5xx, tool_timeout, source_unavailable
KIND_ERROR = "error"                # other 4xx (incl. 409), unexpected outcome or body
KIND_DISABLED = "disabled"          # no agent address

_RETRIABLE_OUTCOMES = ("tool_timeout", "source_unavailable")


def is_enabled() -> bool:
    """Is the platform agent switched on (read from the environment on every call)."""
    return bool(os.getenv("INTERLOCUTOR_URL", "").strip())


def thread_mode_enabled() -> bool:
    """Release A switch: the agent keeps the thread, the bot does not send history (read on every call)."""
    return os.getenv("INTERLOCUTOR_THREAD_MODE", "").strip() == "1"


_chat_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


def chat_lock(chat_id: int) -> asyncio.Lock:
    """One lock per person inside this bot process: a turn and a "forget" of the same chat do not overlap.

    A lock does not span several bot instances; the pilot rollout requires a single instance (F10).
    """
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


def make_turn_id(chat_id: int, turn_ref: Optional[str] = None) -> str:
    """Retry key of one person's message. `turn_ref` is the Telegram message ("m<id>") or press ("c<id>"):
    a re-delivered update then gets the same key. Without it the key is created once per call."""
    return f"{chat_id}:{turn_ref}" if turn_ref else f"{chat_id}:{int(time.time() * 1000)}"


def build_history(pairs: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    """Последние пары «вопрос - ответ» в форме контракта агента (роль user/assistant)."""
    items: List[Dict[str, str]] = []
    for pair in (pairs or [])[-MAX_HISTORY_PAIRS:]:
        question = (pair.get("q") or "").strip()[:MAX_TEXT_CHARS]
        answer = (pair.get("a") or "").strip()[:MAX_TEXT_CHARS]
        if question and answer:
            items.append({"role": "user", "text": question})
            items.append({"role": "assistant", "text": answer})
    return items


class HttpStatusError(Exception):
    """The agent answered with a non-200 status (the class of the failure depends on the code)."""

    def __init__(self, status: int):
        super().__init__(f"http {status}")
        self.status = status


async def _post_json(url: str, token: str, payload: Dict[str, Any], timeout_s: float) -> Optional[Dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url, json=payload, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            if response.status != 200:
                logger.warning("[Interlocutor] http %s", response.status)
                raise HttpStatusError(response.status)
            data = await response.json()
            return data if isinstance(data, dict) else None


async def _delete_json(url: str, token: str, timeout_s: float) -> Optional[Dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.delete(url, headers={"Authorization": f"Bearer {token}"}) as response:
            if response.status != 200:
                logger.warning("[Interlocutor] forget http %s", response.status)
                raise HttpStatusError(response.status)
            data = await response.json()
            return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class TurnResult:
    kind: str
    data: Optional[Dict[str, Any]] = None
    ms: int = 0


def _classify(data: Optional[Dict[str, Any]]) -> str:
    outcome = data.get("outcome") if data else None
    has_text = bool(data) and isinstance(data.get("text"), str) and bool(data["text"].strip())
    if has_text and data.get("fallback_allowed") is False:
        return KIND_AGENT_MESSAGE
    if outcome == "answer" and has_text:
        return KIND_ANSWER
    if outcome == "need_login":
        return KIND_NO_PASS
    if outcome in _RETRIABLE_OUTCOMES:
        return KIND_RETRIABLE
    return KIND_ERROR


def _classify_exception(exc: BaseException) -> str:
    if isinstance(exc, HttpStatusError):
        if exc.status in (401, 403):
            return KIND_NO_PASS  # the pass is not accepted: asking again will not help, sign in
        return KIND_RETRIABLE if exc.status == 429 or exc.status >= 500 else KIND_ERROR
    return KIND_RETRIABLE  # network, timeout, malformed body


async def turn(
    chat_id: int,
    question: str,
    history_pairs: Optional[List[Dict[str, str]]],
    language: str,
    turn_id: str,
    *,
    send_history: bool,
) -> TurnResult:
    """One agent turn with a classified result. `turn_id` is given by the caller so a retry reuses it."""
    from clients.gateway_mcp import gateway_mcp

    url = os.getenv("INTERLOCUTOR_URL", "").strip()
    if not url:
        return TurnResult(KIND_DISABLED)
    try:
        token = await gateway_mcp.access_token_for(chat_id)
    except Exception as exc:
        logger.warning("[Interlocutor] pass lookup failed: %s", type(exc).__name__)
        return TurnResult(KIND_RETRIABLE)
    if not token:
        if gateway_mcp.is_connected(chat_id):
            # Signed in, but the pass could not be refreshed now (a temporary failure): not "no pass".
            logger.info("[Interlocutor] pass refresh failed for a signed-in user")
            return TurnResult(KIND_RETRIABLE)
        logger.info("[Interlocutor] skipped: no url or no pass for user")
        return TurnResult(KIND_NO_PASS)
    payload: Dict[str, Any] = {
        "text": question[:MAX_TEXT_CHARS],
        "turn_id": turn_id,
        "language": language if language in ("ru", "en", "es", "fr") else "en",
        "budget_s": BUDGET_S,
    }
    if send_history:
        payload["history"] = build_history(history_pairs)
    started = time.monotonic()
    try:
        data = await _post_json(url, token, payload, BUDGET_S + 5)
    except (aiohttp.ClientError, TimeoutError, ValueError, HttpStatusError) as exc:
        logger.warning("[Interlocutor] call failed: %s", type(exc).__name__)
        return TurnResult(_classify_exception(exc), None, int((time.monotonic() - started) * 1000))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("[Interlocutor] outcome=%s in %dms", data.get("outcome") if data else None, elapsed_ms)
    return TurnResult(_classify(data), data, elapsed_ms)


async def ask(
    chat_id: int,
    question: str,
    history_pairs: Optional[List[Dict[str, str]]],
    language: str,
) -> Optional[Dict[str, Any]]:
    """Прежний вызов (режим выпуска А выключен). Возвращает ответ агента при коде «answer» с текстом, а также
    сообщение агента, которое нельзя подменять другим ответом (fallback_allowed=false: например, ключ человека
    к модели отклонён и любой другой ответ оплатила бы платформа). Иначе None."""
    result = await turn(
        chat_id, question, history_pairs, language, make_turn_id(chat_id), send_history=True
    )
    if result.kind in (KIND_ANSWER, KIND_AGENT_MESSAGE):
        return result.data
    return None


def _thread_url(turn_url: str) -> str:
    base = turn_url.rstrip("/")
    return (base[: -len("/turn")] if base.endswith("/turn") else base) + "/thread"


async def forget(chat_id: int) -> str:
    """Ask the agent to delete the person's thread now. Returns "forgotten", "refused" (the agent could not
    delete: pass or source problem), "failed" (network/HTTP error) or "skipped" (nothing to call)."""
    from clients.gateway_mcp import gateway_mcp

    url = os.getenv("INTERLOCUTOR_URL", "").strip()
    if not url:
        return "skipped"
    try:
        token = await gateway_mcp.access_token_for(chat_id)
    except Exception as exc:
        logger.warning("[Interlocutor] pass lookup failed: %s", type(exc).__name__)
        return "failed"
    if not token:
        return "failed" if gateway_mcp.is_connected(chat_id) else "skipped"
    try:
        data = await _delete_json(_thread_url(url), token, FORGET_TIMEOUT_S)
    except (aiohttp.ClientError, TimeoutError, ValueError, HttpStatusError) as exc:
        logger.warning("[Interlocutor] forget failed: %s", type(exc).__name__)
        return "failed"
    return "forgotten" if data and data.get("outcome") == "forgotten" else "refused"


def source_labels(sources: Any, limit: int = 2) -> List[str]:
    """Подписи источников для показа: ссылка или путь, иначе имя инструмента."""
    labels: List[str] = []
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        label = source.get("ref") or source.get("tool")
        if isinstance(label, str) and label and label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels
