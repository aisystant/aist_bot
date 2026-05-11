"""
Форматирование Markdown-контента из GitHub для Telegram.

Telegram не поддерживает Markdown-таблицы.
Этот модуль конвертирует стратегические файлы
(DayPlan, WeekPlan, итоги недели) в читаемый HTML.
"""

import html
import re
from typing import Optional


def format_strategy_content(content: str) -> str:
    """Форматирует Markdown-файл стратега для Telegram (HTML).

    - Парсит YAML frontmatter → иконку типа документа
    - Конвертирует таблицы → списки с иконками статусов
    - Heading → <b>bold</b>
    - **bold** → <b>bold</b>
    - `code` → <code>code</code>
    - Убирает --- разделители
    """
    lines = content.split("\n")
    result = []
    in_frontmatter = False
    frontmatter_seen = False
    doc_type = None

    i = 0
    while i < len(lines):
        line = lines[i]

        # --- Frontmatter ---
        if line.strip() == "---":
            if not frontmatter_seen and not result:
                in_frontmatter = True
                frontmatter_seen = True
                i += 1
                continue
            elif in_frontmatter:
                in_frontmatter = False
                # Добавляем иконку типа
                if doc_type:
                    icon = _type_icon(doc_type)
                    if icon:
                        result.append(icon)
                i += 1
                continue
            else:
                # Обычный --- разделитель
                i += 1
                continue

        if in_frontmatter:
            # Извлекаем тип документа
            if line.startswith("type:"):
                doc_type = line.split(":", 1)[1].strip()
            i += 1
            continue

        # --- Таблица ---
        if line.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1
            formatted = _format_table(table_lines)
            if formatted:
                result.append(formatted)
            continue

        # --- Headings ---
        if line.startswith("### "):
            text = _inline_format(line[4:])
            result.append(f"\n<b>{text}</b>")
            i += 1
            continue
        if line.startswith("## "):
            text = _inline_format(line[3:])
            result.append(f"\n<b>{text}</b>")
            i += 1
            continue
        if line.startswith("# "):
            text = _inline_format(line[2:])
            result.append(f"<b>{text}</b>")
            i += 1
            continue

        # --- Обычные строки ---
        result.append(_inline_format(line))
        i += 1

    return "\n".join(result).strip()


def _type_icon(doc_type: str) -> Optional[str]:
    """Иконка по типу документа из frontmatter."""
    icons = {
        "daily-plan": "📋",
        "week-plan": "📅",
        "week-report": "📊",
        "session-prep": "🎯",
    }
    return icons.get(doc_type)


def _inline_format(text: str) -> str:
    """Конвертирует inline Markdown → HTML.

    Если строка содержит HTML-теги (например <details>, <summary>, <b>)
    — защищаем их placeholder'ами ДО html.escape(), восстанавливаем после.
    Это предотвращает двойной escape: &lt;details&gt; вместо <details>.
    """
    _PH = "\x00PH"
    placeholders: list[str] = []

    def _save_html_tag(match: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(match.group(0))
        return f"{_PH}{idx}{_PH}"

    # Защищаем HTML-теги допустимые в Telegram + структурные (<details>, <summary>)
    # Telegram HTML: <b>, <i>, <u>, <s>, <code>, <pre>, <a>, <tg-spoiler>
    # + <details>, <summary> (Telegram поддерживает их в последних версиях)
    text = re.sub(
        r"</?(?:b|i|u|s|code|pre|a|tg-spoiler|details|summary)(?:\s[^>]*)?>",
        _save_html_tag,
        text,
        flags=re.IGNORECASE,
    )

    # Экранируем HTML в оставшемся тексте
    text = html.escape(text)

    # **bold** → <b>bold</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # `code` → <code>code</code>
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    # Восстанавливаем HTML-теги
    for i, tag in enumerate(placeholders):
        text = text.replace(f"{_PH}{i}{_PH}", tag)

    return text


def _format_table(table_lines: list[str]) -> str:
    """Конвертирует Markdown-таблицу в список.

    Распознаёт таблицы РП (с колонками #, РП, Бюджет, ..., Статус)
    и другие таблицы (общий формат).
    """
    if len(table_lines) < 3:
        # Слишком маленькая таблица — пропускаем
        return ""

    # Парсим заголовок
    header_cells = _parse_row(table_lines[0])
    if not header_cells:
        return ""

    # Пропускаем разделитель (строка с ---)
    data_start = 1
    if len(table_lines) > 1 and re.match(r"^\|[\s\-:|]+\|$", table_lines[1]):
        data_start = 2

    # Определяем тип таблицы
    header_lower = [h.lower().strip() for h in header_cells]

    is_rp_table = "#" in header_lower and any(
        kw in " ".join(header_lower) for kw in ["рп", "задача", "статус"]
    )

    rows = []
    for line in table_lines[data_start:]:
        cells = _parse_row(line)
        if cells:
            rows.append(cells)

    if is_rp_table:
        return _format_rp_table(header_cells, rows)
    else:
        return _format_generic_table(header_cells, rows)


def _parse_row(line: str) -> list[str]:
    """Парсит строку таблицы в список ячеек."""
    if not line.startswith("|"):
        return []
    cells = line.split("|")
    # Убираем пустые элементы от начального и конечного |
    cells = [c.strip() for c in cells[1:-1] if c.strip() != ""]
    # Если все ячейки пустые после strip — пропускаем
    if not cells:
        return []
    return cells


def _status_icon(status: str) -> str:
    """Иконка статуса РП."""
    s = status.lower().replace("*", "")
    if "done" in s or "✅" in s:
        return "✅"
    if "in_progress" in s or "in progress" in s or "inprogress" in s or "🔄" in s:
        return "🔄"
    if "pending" in s or "⬜" in s:
        return "⬜"
    return "·"


def _format_rp_table(header: list[str], rows: list[list[str]]) -> str:
    """Форматирует таблицу РП в список."""
    # Находим индексы нужных колонок
    h_lower = [h.lower().strip() for h in header]

    idx_num = _find_col(h_lower, ["#", "№"])
    idx_rp = _find_col(h_lower, ["рп", "задача", "название"])
    idx_budget = _find_col(h_lower, ["бюджет", "время"])
    idx_status = _find_col(h_lower, ["статус", "status"])
    idx_priority = _find_col(h_lower, ["приоритет", "priority"])

    lines = []
    for cells in rows:
        num = _get_cell(cells, idx_num, "").replace("*", "")
        rp = _get_cell(cells, idx_rp, "").replace("*", "")
        budget = _get_cell(cells, idx_budget, "")
        status = _get_cell(cells, idx_status, "")
        priority = _get_cell(cells, idx_priority, "")

        icon = _status_icon(status)

        parts = [f"{icon} #{num} {rp}"]
        if budget:
            parts.append(f"({budget})")
        # Добавляем приоритет если есть красный дедлайн
        if priority and "🔴" in priority:
            parts.append("🔴")

        lines.append(" ".join(parts))

    return "\n".join(lines)


def _format_generic_table(header: list[str], rows: list[list[str]]) -> str:
    """Форматирует обычную таблицу как список."""
    lines = []
    for cells in rows:
        # Берём первые 2-3 значимых ячейки
        clean_cells = [c.replace("*", "") for c in cells if c.strip()]
        if clean_cells:
            lines.append("· " + " — ".join(clean_cells[:3]))
    return "\n".join(lines)


def _find_col(headers: list[str], keywords: list[str]) -> int:
    """Находит индекс колонки по ключевым словам."""
    for i, h in enumerate(headers):
        for kw in keywords:
            if kw in h:
                return i
    return -1


def _get_cell(cells: list[str], idx: int, default: str = "") -> str:
    """Безопасно получает ячейку по индексу."""
    if 0 <= idx < len(cells):
        return cells[idx].strip()
    return default
