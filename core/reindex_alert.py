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


def _target_name(value: object) -> str:
    return _TARGET_NAMES.get(str(value)) or _escaped(value, 60)


def _escaped(value: object, limit: int) -> str:
    text = str(value)
    # Truncate before escaping, so an HTML entity is never cut in half.
    return html.escape(text if len(text) <= limit else text[: limit - 1] + "…")


def render_reindex_alert(body: dict) -> str:
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
        if _UNKNOWN_TIMEOUT.match(error):
            unknown.append(target)
        else:
            errors.append(f"{target}: {_escaped(error, 240)}")

    lines = [f"⚠️ <b>Переиндексация</b>: источник <code>{source}</code>"]
    if unknown:
        targets = "; ".join(unknown[:3])
        if len(unknown) > 3:
            targets += f"; ещё {len(unknown) - 3}"
        lines.extend(
            [
                f"Результат пока неизвестен: {targets}. Ответ не пришёл вовремя; работа могла продолжиться.",
                "Сначала проверьте итог обработки и актуальность индекса. Повторный запуск до проверки не нужен.",
            ]
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
    return "\n".join(lines)
