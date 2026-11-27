"""Render reindex request reports without claiming an unobserved job outcome."""

import html
import re

_UNKNOWN_TIMEOUT = re.compile(r"^timeout \d+(?:ms|s): result unknown\b", re.IGNORECASE)

# Target ids sent by github-integration-service (src/routes/webhook.ts, callReindexTarget);
# the pilot reads names, not service ids (WP-538 F24). Unknown ids pass through escaped.
_TARGET_NAMES = {
    "knowledge": "сервер знаний",
    "personal": "личный индекс знаний",
    "bot_workbook": "рабочая тетрадь в боте",
}


def is_unknown_result(error: object) -> bool:
    """True for a request that timed out without a verdict (the job may still have run)."""
    return bool(_UNKNOWN_TIMEOUT.match(str(error)))


def _repeats_text(count: int) -> str:
    last_two, last = count % 100, count % 10
    if last == 1 and last_two != 11:
        word = "такое сообщение"
    elif 2 <= last <= 4 and not 12 <= last_two <= 14:
        word = "таких сообщения"
    else:
        word = "таких сообщений"
    return f"С прошлого сообщения такого вида пришло ещё {count} {word}, повторы не показывались."


def _target_name(value: object) -> str:
    return _TARGET_NAMES.get(str(value)) or _escaped(value, 60)


def _escaped(value: object, limit: int) -> str:
    text = str(value)
    # Truncate before escaping, so an HTML entity is never cut in half.
    return html.escape(text if len(text) <= limit else text[: limit - 1] + "…")


def render_reindex_alert(body: dict, suppressed: int = 0) -> str:
    source = _escaped(body.get("source", "?"), 120)
    entries = body.get("failed", [])
    if not isinstance(entries, list):
        entries = []
    unknown = []
    errors = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        error = str(entry.get("error", "?"))
        target = _target_name(entry.get("target", "?"))
        if is_unknown_result(error):
            unknown.append(target)
        else:
            errors.append(f"{target}: {_escaped(error, 240)}")

    lines = [f"⚠️ <b>Переиндексация</b>: источник <code>{source}</code>"]
    if any(
        isinstance(entry, dict) and "OpenRouter embeddings error: 401" in str(entry.get("error", ""))
        for entry in entries
    ):
        lines.append(
            "OpenRouter отклонил авторизацию (401). Повторная переиндексация до восстановления доступа не нужна; следующие такие отказы сегодня будут объединены."
        )
    if unknown:
        targets = "; ".join(unknown[:3])
        if len(unknown) > 3:
            targets += f"; ещё {len(unknown) - 3}"
        lines.extend(
            [
                f"Результат пока неизвестен: {targets}. Ответ не пришёл вовремя; работа могла продолжиться.",
                "Повторный запуск до проверки не нужен.",
            ]
        )
        if not errors:
            named = f" {source}" if source != "?" else ""
            lines.append(
                f"Что делать: пока ничего. Если поиск по базе знаний не находит свежие правки, скажите агенту «проверь итог переиндексации{named}»."
            )
    if errors:
        details = "; ".join(errors[:3])
        if len(errors) > 3:
            details += f"; ещё {len(errors) - 3}"
        lines.extend(
            [
                f"Запросы завершились с ошибкой: {details}.",
                "Проверьте причину ошибки и результат обработки перед повторным запуском.",
            ]
        )
    if not unknown and not errors:
        lines.append(
            "Результат пока неизвестен: подробности не получены. Проверьте журнал обработки."
        )
    if suppressed > 0:
        lines.append(_repeats_text(suppressed))
    return "\n".join(lines)

# The request result and the eventual worker result are different observations.
# Record every request before deciding its chat route (WP-625, pilot 26.11).
def reindex_action(body: dict) -> str:
    entries = body.get("failed", [])
    if not isinstance(entries, list):
        return ""
    errors = [
        str(entry.get("error", "")) for entry in entries if isinstance(entry, dict)
    ]
    if any("OpenRouter embeddings error: 401" in error for error in errors):
        return "проверить действительность ключа сервера знаний в панели OpenRouter."
    return ""


async def write_reindex_center(body: dict) -> bool:
    """Acknowledge only a Center write. A retry uses the same Loki timestamp."""
    import asyncio
    import json
    import logging
    import os
    import time
    import uuid

    import aiohttp

    url = os.getenv("REINDEX_CENTER_LOKI_URL", "").strip().rstrip("/")
    user = os.getenv("REINDEX_CENTER_LOKI_USER", "").strip()
    token = os.getenv("REINDEX_CENTER_LOKI_TOKEN", "").strip()
    if not (url and user and token):
        logging.getLogger(__name__).warning("Reindex Center credentials are incomplete")
        return False
    url = url.removesuffix("/loki/api/v1/push") + "/loki/api/v1/push"
    entries = body.get("failed", [])
    errors = (
        [str(entry.get("error", "")) for entry in entries if isinstance(entry, dict)]
        if isinstance(entries, list)
        else []
    )
    result = (
        "failed" if any(not is_unknown_result(error) for error in errors) else "unknown"
    )
    event = {
        "schema_version": 1,
        "event": "notification",
        "phase": "request",
        "request_result": result,
        "source_name": str(body.get("source") or "?")[:120],
        "title": "Результат запроса переиндексации",
        "detail": "Запрос ответил с ошибкой."
        if result == "failed"
        else "Итог работы пока неизвестен.",
        "correlation_id": "knowledge-reindex:request:"
        + str(body.get("event_id") or uuid.uuid4()),
    }
    payload = {
        "streams": [
            {
                "stream": {
                    "job": "notification-center",
                    "source": "knowledge-reindex",
                    "severity": "warning",
                    "needs_decision": "false",
                },
                "values": [
                    [str(time.time_ns()), json.dumps(event, ensure_ascii=False)]
                ],
            }
        ]
    }
    timeout = aiohttp.ClientTimeout(total=4)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(2):
            try:
                async with session.post(
                    url, json=payload, auth=aiohttp.BasicAuth(user, token)
                ) as response:
                    if response.status in (200, 204):
                        return True
                    if response.status < 500 and response.status != 429:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                logging.getLogger(__name__).warning(
                    "Reindex Center write failed: %s", type(error).__name__
                )
    return False


async def route_reindex_alert(body: dict) -> tuple[dict, int]:
    """All events reach the digest. Actionable alerts share one durable daily key."""
    import logging
    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not await write_reindex_center(body):
        return {"ok": False, "reason": "center_unavailable"}, 503
    action = reindex_action(body)
    if not action:
        return {"ok": True, "route": "digest"}, 200
    try:
        chat_id = int(os.environ["OPS_ALERT_CHAT_ID"])
        from core.notification_service import CLASS_OPS_ALERT, enqueue

        from datetime import time, timedelta

        now = datetime.now(ZoneInfo("Asia/Nicosia"))
        day = now.date().isoformat()
        expires = datetime.combine(
            now.date() + timedelta(days=1), time(), tzinfo=now.tzinfo
        )
        key = f"reindex-action:{day}"
        text = render_reindex_alert(body) + "\nЧто делать: " + action
        outcome = await enqueue(
            chat_id,
            CLASS_OPS_ALERT,
            {"text": text, "format": "html", "expires_at": expires.isoformat()},
            dedup_key=key,
            journal_key=key,
            journal_type="ops_alert",
            dedup_hours=24,
        )
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Reindex action enqueue failed: %s", type(error).__name__
        )
        return {"ok": False, "reason": "action_queue_unavailable"}, 503
    return {"ok": True, "route": outcome["status"]}, 200
