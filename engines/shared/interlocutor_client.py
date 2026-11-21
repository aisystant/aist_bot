"""Клиент разговорного агента платформы (iwe-interlocutor, РП-262 Ф10).

Бот передаёт только текущий вопрос и пропуск человека: нить хранит агент. Временный сбой
можно повторить с тем же turn_id — агент вернёт сохранённый успешный ответ либо выполнит ход
один раз под блокировкой нити. Отсутствие пропуска отделено от сбоя, чтобы окно не подменяло
ответ агента собственным ответом при временной недоступности.

Флаг: INTERLOCUTOR_URL — адрес POST /v1/turn; пусто = агент выключен.
В журнал пишутся только исход, HTTP-статус, попытка и длительность, без текста и пропусков.
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import aiohttp

logger = logging.getLogger(__name__)

BUDGET_S = 55.0
MAX_TEXT_CHARS = 4000
MAX_RETRY_AFTER_S = 5.0


def is_enabled() -> bool:
    """Is the platform agent switched on (read from the environment on every call)."""
    return bool(os.getenv("INTERLOCUTOR_URL", "").strip())


def retryable(result: Optional[Dict[str, Any]]) -> bool:
    """Only a temporary failure may be retried; login and a forbidden fallback may not."""
    if not result or result.get("fallback_allowed") is False:
        return False
    outcome = result.get("outcome")
    if outcome in ("call_failed", "tool_timeout", "source_unavailable"):
        return True
    if outcome == "http_error":
        status = result.get("status")
        return status in (408, 429) or isinstance(status, int) and status >= 500
    return False


def retry_delay_s(result: Dict[str, Any]) -> Optional[float]:
    """A short Retry-After is honoured; a longer wait is left for the person's manual retry."""
    if result.get("outcome") != "http_error" or result.get("status") != 429:
        return 0.0
    delay = result.get("retry_after_s", 0.0)
    return delay if delay <= MAX_RETRY_AFTER_S else None


async def _post_json(url: str, token: str, payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers={"Authorization": f"Bearer {token}"}) as response:
            if response.status != 200:
                retry_after = response.headers.get("Retry-After", "0")
                try:
                    retry_after_s = max(0.0, float(retry_after))
                except ValueError:
                    retry_after_s = 0.0
                logger.warning("[Interlocutor] http status=%s", response.status)
                if response.status in (401, 403):
                    return {"outcome": "need_login", "status": response.status}
                return {
                    "outcome": "http_error",
                    "status": response.status,
                    "retry_after_s": retry_after_s,
                }
            data = await response.json()
            if isinstance(data, dict):
                return data
            return {"outcome": "source_unavailable", "text": ""}


async def ask(chat_id: int, question: str, language: str, turn_id: str, *, attempt: int = 1) -> Dict[str, Any]:
    """One attempt of one turn. The caller retries a temporary failure with the same turn_id."""
    from clients.gateway_mcp import gateway_mcp

    url = os.getenv("INTERLOCUTOR_URL", "").strip()
    if not url:
        return {"outcome": "disabled"}
    try:
        token = await gateway_mcp.access_token_for(chat_id)
    except Exception as exc:
        logger.warning("[Interlocutor] pass failed error=%s", type(exc).__name__)
        return {"outcome": "call_failed"}
    if not token:
        logger.info("[Interlocutor] outcome=no_pass")
        return {"outcome": "no_pass"}
    payload = {
        "text": question[:MAX_TEXT_CHARS],
        "turn_id": turn_id,
        "language": language if language in ("ru", "en", "es", "fr") else "en",
        "history": [],
        "budget_s": BUDGET_S,
    }
    started = time.monotonic()
    try:
        data = await _post_json(url, token, payload, BUDGET_S + 5)
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, ValueError) as exc:
        logger.warning("[Interlocutor] outcome=call_failed error=%s", type(exc).__name__)
        return {"outcome": "call_failed"}
    duration_ms = int((time.monotonic() - started) * 1000)
    outcome = data.get("outcome")
    logger.info(
        "[Interlocutor] outcome=%s attempt=%s status=%s duration_ms=%s",
        outcome,
        attempt,
        data.get("status", 200),
        duration_ms,
    )
    if outcome == "answer" and not (isinstance(data.get("text"), str) and data["text"].strip()):
        return {"outcome": "source_unavailable", "text": ""}
    return data


def _thread_url() -> str:
    turn_url = os.getenv("INTERLOCUTOR_URL", "").strip()
    parts = urlsplit(turn_url)
    path = parts.path.rstrip('/').removesuffix("/v1/turn") + "/v1/thread"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


async def delete_thread(chat_id: int) -> Dict[str, Any]:
    """Delete the agent-held thread; a missing pass is not a successful deletion."""
    from clients.gateway_mcp import gateway_mcp

    if not is_enabled():
        return {"outcome": "disabled"}
    try:
        token = await gateway_mcp.access_token_for(chat_id)
    except Exception as exc:
        logger.warning("[Interlocutor] delete pass failed error=%s", type(exc).__name__)
        return {"outcome": "call_failed"}
    if not token:
        logger.info("[Interlocutor] delete outcome=no_pass")
        return {"outcome": "no_pass"}
    started = time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=15.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.delete(_thread_url(), headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status != 200:
                    logger.warning("[Interlocutor] delete http status=%s", response.status)
                    if response.status in (401, 403):
                        return {"outcome": "need_login", "status": response.status}
                    return {"outcome": "unavailable", "status": response.status}
                data = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, ValueError) as exc:
        logger.warning("[Interlocutor] delete outcome=call_failed error=%s", type(exc).__name__)
        return {"outcome": "call_failed"}
    outcome = data.get("outcome") if isinstance(data, dict) else "unavailable"
    logger.info(
        "[Interlocutor] delete outcome=%s duration_ms=%s",
        outcome,
        int((time.monotonic() - started) * 1000),
    )
    return data if isinstance(data, dict) else {"outcome": "unavailable"}


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
