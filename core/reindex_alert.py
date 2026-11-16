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
            lines.append(
                f"Что делать: пока ничего. Если поиск по базе знаний не находит свежие правки, скажите агенту «проверь итог переиндексации {source}»."
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
