"""
Модуль самознания бота (Self-Knowledge).

Трёхуровневая модель скорости ответа:
- L0 (проекция, ~10ms): YAML-проекция, сгенерированная Синхронизатором из Pack.
  Файл: config/self_knowledge_projection.yaml (read-only, auto-generated).
- L1 (кеш, ~100ms): описания и FAQ из проекции/Pack + граф сервисов из реестра.
  Обновляется раз в день или при рестарте.
- L2 (MCP, ~1-3s): запрос к Pack через MCP + быстрая модель. (TODO)
- L3 (полный, ~3-8s): MCP guides + knowledge + Sonnet. (существующий pipeline)

Приоритет загрузки: L0 проекция → локальный Pack → GitHub raw URL.

Source-of-truth: секция 4.1.1 в DP.AISYS.014 (PACK-digital-platform).
Синхронизатор: DS-synchronizer/scripts/pack-project.sh → projection YAML.

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

import yaml

from core.registry import registry
from i18n import t

logger = logging.getLogger(__name__)

# L0: Проекция из Синхронизатора (приоритетный источник)
_PROJECTION_PATH = Path(__file__).parent.parent / "config" / "self_knowledge_projection.yaml"

# Fallback: Pack-паспорт бота (source-of-truth)
# Dev: Pack-репо рядом с ботом ~/Github/PACK-digital-platform/
_PACK_PATH = Path(__file__).parent.parent.parent / "PACK-digital-platform" / \
    "pack" / "digital-platform" / "02-domain-entities" / "DP.AISYS.014-aist-bot.md"

# Fallback: Prod (Railway/Neon): загрузить с GitHub (репо публичный)
_GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/TserenTserenov/PACK-digital-platform"
    "/main/pack/digital-platform/02-domain-entities/DP.AISYS.014-aist-bot.md"
)

# Кеш
_scenarios: list[dict] = []
_faq: list[dict] = []
_troubleshooting: list[dict] = []
_identity: dict = {}
_integrations: str = ""
_loaded: bool = False
_cache: dict[str, str] = {}


def _load_projection() -> Optional[dict]:
    """L0: Загрузить YAML-проекцию из Синхронизатора (приоритетный путь)."""
    if not _PROJECTION_PATH.exists():
        return None

    try:
        with open(_PROJECTION_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data and isinstance(data, dict) and '_meta' in data:
            logger.info(
                f"Self-knowledge: loaded from projection "
                f"(synced_at={data['_meta'].get('synced_at', '?')})"
            )
            return data
    except Exception as e:
        logger.warning(f"Failed to read projection: {e}")

    return None


def _fetch_content() -> Optional[str]:
    """Fallback: загрузить паспорт бота из Pack (локально или GitHub)."""
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
    """Загрузить самознание: L0 проекция → fallback Pack markdown (lazy, один раз)."""
    global _scenarios, _faq, _troubleshooting, _identity, _integrations, _loaded

    if _loaded:
        return

    _loaded = True

    # --- L0: Проекция (приоритет) ---
    projection = _load_projection()
    if projection:
        identity_text = projection.get('identity', '')
        scenarios_text = projection.get('scenarios', '')
        faq_text = projection.get('faq', '')

        _identity.update(_parse_identity(identity_text))
        _scenarios.extend(_parse_scenarios_table_from_text(scenarios_text))
        _faq.extend(_parse_faq_table_from_text(faq_text))
        troubleshooting_text = projection.get('troubleshooting', '')
        if troubleshooting_text:
            _troubleshooting.extend(_parse_faq_table_from_text(troubleshooting_text))
        _integrations = projection.get('integrations', '')

        logger.info(
            f"Self-knowledge loaded from projection: "
            f"{len(_scenarios)} scenarios, {len(_faq)} FAQ, {len(_troubleshooting)} troubleshooting"
        )
        return

    # --- Fallback: Pack markdown ---
    content = _fetch_content()
    if not content:
        logger.warning("Self-knowledge: no content loaded (projection + Pack + GitHub all failed)")
        return

    _identity.update(_parse_identity_from_pack(content))
    _scenarios.extend(_parse_scenarios_table(content))
    _faq.extend(_parse_faq_table(content))
    _troubleshooting.extend(_parse_troubleshooting_table(content))

    logger.info(
        f"Self-knowledge loaded from Pack fallback: "
        f"{len(_scenarios)} scenarios, {len(_faq)} FAQ, {len(_troubleshooting)} troubleshooting"
    )


def _parse_identity(text: str) -> dict:
    """Извлечь идентичность бота из текста (projection или Pack section)."""
    result = {}

    m = re.search(r'\*\*Имя:\*\*\s*(.+)', text)
    if m:
        result['name'] = m.group(1).strip()

    m = re.search(r'\*\*Назначение \(ru\):\*\*\s*(.+)', text)
    if m:
        result['purpose_ru'] = m.group(1).strip()

    m = re.search(r'\*\*Назначение \(en\):\*\*\s*(.+)', text)
    if m:
        result['purpose_en'] = m.group(1).strip()

    m = re.search(r'\*\*Как задать вопрос \(ru\):\*\*\s*(.+)', text)
    if m:
        result['ask_ru'] = m.group(1).strip()

    m = re.search(r'\*\*Как задать вопрос \(en\):\*\*\s*(.+)', text)
    if m:
        result['ask_en'] = m.group(1).strip()

    return result


def _parse_identity_from_pack(content: str) -> dict:
    """Извлечь идентичность из полного Pack markdown (fallback)."""
    match = re.search(r'##### Идентичность бота\s*\n(.*?)(?=\n##### |\Z)', content, re.DOTALL)
    if match:
        return _parse_identity(match.group(1))
    return _parse_identity(content)


def _parse_scenarios_from_rows(rows: list[list[str]]) -> list[dict]:
    """Распарсить строки таблицы сценариев. Только ✅ попадают в L1."""
    scenarios = []
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


def _parse_faq_from_rows(rows: list[list[str]]) -> list[dict]:
    """Распарсить строки таблицы FAQ."""
    faq = []
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


def _parse_scenarios_table_from_text(text: str) -> list[dict]:
    """Извлечь сценарии из текста секции (projection path)."""
    return _parse_scenarios_from_rows(_parse_md_table(text))


def _parse_faq_table_from_text(text: str) -> list[dict]:
    """Извлечь FAQ из текста секции (projection path)."""
    return _parse_faq_from_rows(_parse_md_table(text))


def _parse_scenarios_table(content: str) -> list[dict]:
    """Извлечь таблицу сценариев из полного Pack markdown (fallback)."""
    match = re.search(r'##### Сценарии\s*\n(.*?)(?=\n##### |\n### |\Z)', content, re.DOTALL)
    if not match:
        return []
    return _parse_scenarios_from_rows(_parse_md_table(match.group(1)))


def _parse_faq_table(content: str) -> list[dict]:
    """Извлечь FAQ из полного Pack markdown (fallback)."""
    match = re.search(r'##### FAQ\s*\n(.*?)(?=\n##### |\n### |\n## |\Z)', content, re.DOTALL)
    if not match:
        return []
    return _parse_faq_from_rows(_parse_md_table(match.group(1)))


def _parse_troubleshooting_table(content: str) -> list[dict]:
    """Извлечь Troubleshooting из полного Pack markdown."""
    match = re.search(r'##### Troubleshooting.*?\n(.*?)(?=\n### |\n## |\Z)', content, re.DOTALL)
    if not match:
        return []
    return _parse_faq_from_rows(_parse_md_table(match.group(1)))


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

    # --- Troubleshooting ---
    if _troubleshooting:
        lines.append("\n## Решение проблем" if is_ru else "\n## Troubleshooting")
        for item in _troubleshooting:
            q = item.get(f'question_{lang}') or item.get('question_ru', '')
            a = item.get(f'answer_{lang}') or item.get('answer_ru', '')
            if q and a:
                lines.append(f"\nПроблема: {q}" if is_ru else f"\nProblem: {q}")
                lines.append(f"Решение: {a}" if is_ru else f"Solution: {a}")

    # --- Интеграции (технический стек) ---
    if _integrations:
        lines.append("\n## Интеграции" if is_ru else "\n## Integrations")
        lines.append(_integrations.strip())

    # --- Программы (DP.ARCH.002 § 12 — конверсионная воронка C2) ---
    from config.settings import PLATFORM_URLS

    if is_ru:
        lines.append("\n## Программы обучения")
        lines.append(
            "Если пользователь спрашивает «что дальше», «как продолжить», "
            "«хочу углубиться» или завершил марафон — рекомендуй программы:\n"
            f"1. *Личное развитие (ЛР)* — вводная программа, подходит всем: {PLATFORM_URLS['lr']}\n"
            f"2. *Рабочее развитие (РР)* — для менеджеров и инженеров: {PLATFORM_URLS['rr']}\n"
            f"3. *Исследовательское развитие (ИР)* — для исследователей: {PLATFORM_URLS['ir']}\n"
            f"\nРасписание событий: {PLATFORM_URLS['schedule']}\n"
            f"Руководства (бесплатно): {PLATFORM_URLS['guides']}\n"
            "\nНачинай рекомендацию с ЛР — это точка входа для большинства."
        )
    else:
        lines.append("\n## Learning Programs")
        lines.append(
            "If user asks 'what's next', 'how to continue', "
            "'want to go deeper' or completed the marathon — recommend programs:\n"
            f"1. *Personal Development (ЛР)* — intro program, fits everyone: {PLATFORM_URLS['lr']}\n"
            f"2. *Professional Development (РР)* — for managers and engineers: {PLATFORM_URLS['rr']}\n"
            f"3. *Research Development (ИР)* — for researchers: {PLATFORM_URLS['ir']}\n"
            f"\nEvent schedule: {PLATFORM_URLS['schedule']}\n"
            f"Guides (free): {PLATFORM_URLS['guides']}\n"
            "\nStart recommendation with ЛР — it's the entry point for most people."
        )

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

    # Поиск и по FAQ, и по Troubleshooting (обе таблицы с одинаковой структурой)
    for item in _faq + _troubleshooting:
        keywords = item.get('keywords', [])
        if not keywords:
            continue

        matched = sum(1 for kw in keywords if kw in q_lower)
        if matched > best_score:
            best_score = matched
            best_item = item

    if best_item and best_score >= 1:
        answer = best_item.get(f'answer_{lang}') or best_item.get('answer_ru', '')
        # Конвертировать литеральные \n маркеры из Pack в реальные переносы строк
        return answer.replace('\\n', '\n')

    return None


def get_scenario_names(lang: str = 'ru') -> list[str]:
    """Получить названия всех сценариев (для L1 кеша)."""
    _parse_pack()
    return [sc['name'] for sc in _scenarios]


def invalidate_cache():
    """Сбросить кеш (для ежедневного обновления или тестов)."""
    global _loaded, _scenarios, _faq, _troubleshooting, _identity, _integrations, _cache
    _loaded = False
    _scenarios = []
    _faq = []
    _troubleshooting = []
    _identity = {}
    _integrations = ""
    _cache = {}
