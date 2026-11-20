"""Клиент разговорного агента платформы (iwe-interlocutor, РП-262 Ф4).

Бот остаётся тонким окном: при включённом флаге он отправляет вопрос агенту вместе с пропуском
самого человека и показывает готовый ответ. Права проверяет шлюз, роль и инструменты определяет
агент. Любой сбой агента возвращает None, и бот отвечает прежним путём (запасной до этапа 6).

Флаг (без перевыкладки кода, откат тем же способом):
  INTERLOCUTOR_URL     - адрес POST /v1/turn; пусто = выключено для всех.
  Кому отвечает агент, решает вызывающий: всем, кто прошёл проверку подписки (WP-262 F4, 20.11).

В журнал пишутся только длительность и код исхода: вопросы, ответы и пропуск не пишутся.
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

BUDGET_S = 55.0
MAX_HISTORY_PAIRS = 5
MAX_TEXT_CHARS = 4000


def is_enabled() -> bool:
    """Is the platform agent switched on (read from the environment on every call)."""
    return bool(os.getenv("INTERLOCUTOR_URL", "").strip())


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


async def _post_json(url: str, token: str, payload: Dict[str, Any], timeout_s: float) -> Optional[Dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url, json=payload, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            if response.status != 200:
                logger.warning("[Interlocutor] http %s", response.status)
                return None
            data = await response.json()
            return data if isinstance(data, dict) else None


async def ask(
    chat_id: int,
    question: str,
    history_pairs: Optional[List[Dict[str, str]]],
    language: str,
) -> Optional[Dict[str, Any]]:
    """Один ход. Возвращает ответ агента при коде «answer» с текстом, а также сообщение агента,
    которое нельзя подменять другим ответом (fallback_allowed=false: например, ключ человека
    к модели отклонён и любой другой ответ оплатила бы платформа). Иначе None."""
    from clients.gateway_mcp import gateway_mcp

    url = os.getenv("INTERLOCUTOR_URL", "").strip()
    token = await gateway_mcp.access_token_for(chat_id)
    if not url or not token:
        logger.info("[Interlocutor] skipped: no url or no pass for user")
        return None
    payload = {
        "text": question[:MAX_TEXT_CHARS],
        "turn_id": f"{chat_id}:{int(time.time() * 1000)}",
        "language": language if language in ("ru", "en", "es", "fr") else "en",
        "history": build_history(history_pairs),
        "budget_s": BUDGET_S,
    }
    started = time.monotonic()
    try:
        data = await _post_json(url, token, payload, BUDGET_S + 5)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning("[Interlocutor] call failed: %s", type(exc).__name__)
        return None
    outcome = data.get("outcome") if data else None
    logger.info("[Interlocutor] outcome=%s in %dms", outcome, int((time.monotonic() - started) * 1000))
    has_text = bool(data) and isinstance(data.get("text"), str) and bool(data["text"].strip())
    if has_text and (outcome == "answer" or data.get("fallback_allowed") is False):
        return data
    return None


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
