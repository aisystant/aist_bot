"""
Модуль самознания бота (Self-Knowledge).

Трёхуровневая модель скорости ответа:
- L1 (кеш, ~100ms): описания и FAQ из Pack + граф сервисов из реестра.
  Обновляется раз в день или при рестарте.
- L2 (MCP, ~1-3s): запрос к Pack через MCP + быстрая модель. (TODO)
- L3 (полный, ~3-8s): MCP guides + knowledge + Sonnet. (существующий pipeline)

Source-of-truth: секция 4.1.1 в DP.AISYS.014 (PACK-digital-platform).
Загрузка: локальный файл (dev) → GitHub raw URL (prod/Railway).

Использование:
    from core.self_knowledge import get_self_knowledge, match_faq

    text = get_self_knowledge('ru')       # Полный текст для system prompt
    answer = match_faq('как начать', 'ru') # Проверить FAQ (L1 кеш)
"""

import logging
import re
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from core.registry import registry
from i18n import t

logger = logging.getLogger(__name__)

# Путь к Pack-паспорту бота (source-of-truth)
# Dev: Pack-репо рядом с ботом ~/Github/PACK-digital-platform/
_PACK_PATH = Path(__file__).parent.parent.parent / "PACK-digital-platform" / \
    "pack" / "digital-platform" / "02-domain-entities" / "DP.AISYS.014-aist-bot.md"

# Prod (Railway): загрузить с GitHub (репо публичный)
_GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/TserenTserenov/PACK-digital-platform"
    "/main/pack/digital-platform/02-domain-entities/DP.AISYS.014-aist-bot.md"
)

# Кеш
_scenarios: list[dict] = []
_faq: list[dict] = []
_identity: dict = {}
_loaded: bool = False
_cache: dict[str, str] = {}


def _fetch_content() -> Optional[str]:
    """Загрузить паспорт бота: сначала локально, потом с GitHub."""
    # 1. Локальный файл (dev)
    if _PACK_PATH.exists():
        try:
            content = _PACK_PATH.read_text(encoding="utf-8")
            logger.info(f"Self-knowledge: loaded from local Pack: {_PACK_PATH}")
            return content
        except Exception as e:
            logger.warning(f"Failed to read local Pack: {e}")

    # 2. GitHub raw URL (prod)
    try:
        req = Request(_GITHUB_RAW_URL, headers={"User-Agent": "AIST-Bot"})
        with urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
        logger.info(f"Self-knowledge: loaded from GitHub ({len(content)} chars)")
        return content
    except (URLError, OSError) as e:
        logger.error(f"Failed to fetch Pack from GitHub: {e}")

    return None


def _parse_pack() -> None:
    """Парсить секцию 4.1.1 из Pack-паспорта (lazy, один раз)."""
    global _scenarios, _faq, _identity, _loaded

    if _loaded:
        return

    _loaded = True

    content = _fetch_content()
    if not content:
        logger.warning("Self-knowledge: no content loaded (local + GitHub both failed)")
        return

    # --- Извлечь секцию между "##### Идентичность бота" и "##### Сценарии" ---
    _identity.update(_parse_identity(content))

    # --- Извлечь таблицу сценариев ---
    _scenarios.extend(_parse_scenarios_table(content))

    # --- Извлечь таблицу FAQ ---
    _faq.extend(_parse_faq_table(content))

    logger.info(
        f"Self-knowledge loaded from Pack: "
        f"{len(_scenarios)} scenarios, {len(_faq)} FAQ items"
    )


def _parse_identity(content: str) -> dict:
    """Извлечь идентичность бота из markdown."""
    result = {}

    # Имя
    m = re.search(r'\*\*Имя:\*\*\s*(.+)', content)
    if m:
        result['name'] = m.group(1).strip()

    # Назначение (ru)
    m = re.search(r'\*\*Назначение \(ru\):\*\*\s*(.+)', content)
    if m:
        result['purpose_ru'] = m.group(1).strip()

    # Назначение (en)
    m = re.search(r'\*\*Назначение \(en\):\*\*\s*(.+)', content)
    if m:
        result['purpose_en'] = m.group(1).strip()

    # Как задать вопрос (ru)
    m = re.search(r'\*\*Как задать вопрос \(ru\):\*\*\s*(.+)', content)
    if m:
        result['ask_ru'] = m.group(1).strip()

    # Как задать вопрос (en)
    m = re.search(r'\*\*Как задать вопрос \(en\):\*\*\s*(.+)', content)
    if m:
        result['ask_en'] = m.group(1).strip()

    return result


def _parse_scenarios_table(content: str) -> list[dict]:
    """Извлечь таблицу сценариев из markdown.

    Формат: # | Сценарий | Команда | Статус | Описание (ru) | Описание (en)
    Только строки со статусом ✅ попадают в L1 кеш.
    """
    scenarios = []

    # Найти секцию "##### Сценарии"
    match = re.search(r'##### Сценарии\s*\n(.*?)(?=\n##### |\n### |\Z)', content, re.DOTALL)
    if not match:
        return scenarios

    table_text = match.group(1)
    rows = _parse_md_table(table_text)

    for row in rows:
        if len(row) >= 6:
            status = row[3].strip()
            if '✅' not in status:
                continue
            scenarios.append({
                'name': row[1].strip(),
                'command': row[2].strip(),
                'status': status,
                'description_ru': row[4].strip(),
                'description_en': row[5].strip(),
            })

    return scenarios


def _parse_faq_table(content: str) -> list[dict]:
    """Извлечь таблицу FAQ из markdown."""
    faq = []

    # Найти секцию "##### FAQ"
    match = re.search(r'##### FAQ\s*\n(.*?)(?=\n### |\n## |\Z)', content, re.DOTALL)
    if not match:
        return faq

    table_text = match.group(1)
    rows = _parse_md_table(table_text)

    for row in rows:
        if len(row) >= 6:
            keywords_str = row[3].strip()
            keywords = [kw.strip() for kw in keywords_str.split(',') if kw.strip()]
            faq.append({
                'question_ru': row[1].strip(),
                'question_en': row[2].strip(),
                'keywords': keywords,
                'answer_ru': row[4].strip(),
                'answer_en': row[5].strip(),
            })

    return faq


def _parse_md_table(text: str) -> list[list[str]]:
    """Парсить markdown-таблицу, пропуская заголовок и разделитель."""
    rows = []
    lines = text.strip().split('\n')
    data_started = False

    for line in lines:
        line = line.strip()
        if not line.startswith('|'):
            continue

        # Пропустить разделитель (|---|---|...)
        if re.match(r'^\|[\s\-:|]+\|$', line):
            data_started = True
            continue

        # Пропустить заголовок (первая строка до разделителя)
        if not data_started:
            continue

        cells = [cell.strip() for cell in line.split('|')]
        # Убрать пустые ячейки от начального и конечного |
        if cells and cells[0] == '':
            cells = cells[1:]
        if cells and cells[-1] == '':
            cells = cells[:-1]

        if cells:
            rows.append(cells)

    return rows


def get_self_knowledge(lang: str = 'ru') -> str:
    """Собрать полный текст самознания бота.

    Мержит данные из Pack (сценарии, FAQ) с ServiceRegistry (команды, иконки).
    Кешируется по языку.
    """
    if lang in _cache:
        return _cache[lang]

    _parse_pack()

    lines = []
    is_ru = lang == 'ru'

    # --- Идентичность ---
    bot_name = _identity.get('name', 'AIST Bot')
    purpose = _identity.get(f'purpose_{lang}') or _identity.get('purpose_ru', '')
    ask = _identity.get(f'ask_{lang}') or _identity.get('ask_ru', '')

    lines.append(f"# {bot_name}")
    if purpose:
        lines.append(purpose)
    if ask:
        lines.append(f"\n{ask}")

    # --- Сценарии (из Pack + иконки из реестра) ---
    registry_services = {s.id: s for s in registry.get_all()}
    registry_by_cmd = {}
    for s in registry.get_all():
        if s.command:
            registry_by_cmd[s.command] = s
        for cmd in s.commands:
            registry_by_cmd[cmd] = s

    lines.append("\n## Сценарии" if is_ru else "\n## Scenarios")

    for sc in _scenarios:
        cmd = sc['command']
        desc = sc.get(f'description_{lang}') or sc.get('description_ru', '')

        # Найти иконку из реестра
        icon = ""
        reg_svc = registry_by_cmd.get(cmd)
        if reg_svc:
            icon = f"{reg_svc.icon} "

        lines.append(f"\n### {icon}{sc['name']} ({cmd})" if cmd else f"\n### {icon}{sc['name']}")
        if desc:
            lines.append(desc)

    # --- FAQ ---
    if _faq:
        lines.append("\n## Частые вопросы" if is_ru else "\n## FAQ")
        for item in _faq:
            q = item.get(f'question_{lang}') or item.get('question_ru', '')
            a = item.get(f'answer_{lang}') or item.get('answer_ru', '')
            if q and a:
                lines.append(f"\nВ: {q}" if is_ru else f"\nQ: {q}")
                lines.append(f"О: {a}" if is_ru else f"A: {a}")

    result = "\n".join(lines)
    _cache[lang] = result
    return result


def match_faq(question: str, lang: str = 'ru') -> Optional[str]:
    """Проверить, совпадает ли вопрос с FAQ (L1 кеш).

    Скоринг по количеству совпавших keywords. Возвращает лучший ответ или None.
    """
    _parse_pack()

    q_lower = question.lower()

    best_item = None
    best_score = 0

    for item in _faq:
        keywords = item.get('keywords', [])
        if not keywords:
            continue

        matched = sum(1 for kw in keywords if kw in q_lower)
        if matched > best_score:
            best_score = matched
            best_item = item

    if best_item and best_score >= 1:
        return best_item.get(f'answer_{lang}') or best_item.get('answer_ru', '')

    return None


def get_scenario_names(lang: str = 'ru') -> list[str]:
    """Получить названия всех сценариев (для L1 кеша)."""
    _parse_pack()
    return [sc['name'] for sc in _scenarios]


def invalidate_cache():
    """Сбросить кеш (для ежедневного обновления или тестов)."""
    global _loaded, _scenarios, _faq, _identity, _cache
    _loaded = False
    _scenarios = []
    _faq = []
    _identity = {}
    _cache = {}
