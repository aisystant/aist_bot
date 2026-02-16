"""
Инструменты для Claude-консультанта (tool_use).

Определяет tools для Anthropic API и executor для их вызова.
Используется в handle_question_with_tools() для T2+ пользователей.

Tools:
- search_knowledge: поиск в базе знаний (Pack, guides, DS)
- search_guides: поиск по гайдам
- read_digital_twin: чтение данных ЦД пользователя

Архитектурное решение: DP.ARCH.002 (Тиры обслуживания).
"""

import json
from typing import Any, Dict, List, Optional

from config import get_logger
from clients import mcp_knowledge, digital_twin

logger = get_logger(__name__)


# =============================================================================
# TOOL DEFINITIONS (Anthropic API format)
# =============================================================================

TOOL_SEARCH_KNOWLEDGE = {
    "name": "search_knowledge",
    "description": (
        "Семантический поиск по базе знаний Aisystant. "
        "Включает Pack-источники (системное мышление, личное развитие, "
        "цифровая платформа), руководства и документацию. "
        "Используй для ответа на предметные вопросы пользователя."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Поисковый запрос на естественном языке"
            },
            "limit": {
                "type": "integer",
                "description": "Максимум результатов (1-10)",
                "default": 5
            },
            "source_type": {
                "type": "string",
                "enum": ["pack", "guides", "ds"],
                "description": "Фильтр по типу источника: pack (знания), guides (гайды), ds (процессы)"
            }
        },
        "required": ["query"]
    }
}

TOOL_SEARCH_GUIDES = {
    "name": "search_guides",
    "description": (
        "Поиск по образовательным гайдам Aisystant. "
        "Гайды содержат пошаговые инструкции, примеры и практики. "
        "Используй когда нужен практический совет или пошаговая инструкция."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Поисковый запрос"
            },
            "limit": {
                "type": "integer",
                "description": "Максимум результатов (1-10)",
                "default": 5
            }
        },
        "required": ["query"]
    }
}

TOOL_READ_DIGITAL_TWIN = {
    "name": "read_digital_twin",
    "description": (
        "Чтение данных Цифрового Двойника (ЦД) пользователя. "
        "ЦД содержит: профиль (имя, занятие), цели обучения, "
        "самооценку по компетенциям, текущие проблемы, контекст. "
        "Используй для персонализации ответа, когда нужно знать "
        "цели, проблемы или контекст пользователя."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Путь к данным. Примеры: "
                    "'1_declarative' (весь декларативный раздел), "
                    "'1_declarative/1_2_goals' (цели), "
                    "'1_declarative/1_3_selfeval' (самооценка), "
                    "'1_declarative/1_4_context' (контекст). "
                    "Пустая строка = весь профиль."
                )
            }
        },
        "required": ["path"]
    }
}


def get_tools_for_tier(has_digital_twin: bool) -> List[Dict[str, Any]]:
    """Возвращает набор tools в зависимости от тира пользователя.

    T1 (без ЦД): search_knowledge, search_guides
    T2+ (с ЦД): + read_digital_twin
    """
    tools = [TOOL_SEARCH_KNOWLEDGE, TOOL_SEARCH_GUIDES]
    if has_digital_twin:
        tools.append(TOOL_READ_DIGITAL_TWIN)
    return tools


# =============================================================================
# TOOL EXECUTOR
# =============================================================================

async def execute_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    telegram_user_id: Optional[int] = None,
) -> str:
    """Исполняет tool call, проксируя к существующим MCP-клиентам.

    Args:
        tool_name: имя инструмента
        tool_input: параметры вызова
        telegram_user_id: ID пользователя (для DT)

    Returns:
        Результат в виде строки (JSON или текст)
    """
    if tool_name == "search_knowledge":
        return await _exec_search_knowledge(tool_input)
    elif tool_name == "search_guides":
        return await _exec_search_guides(tool_input)
    elif tool_name == "read_digital_twin":
        return await _exec_read_digital_twin(tool_input, telegram_user_id)
    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})


async def _exec_search_knowledge(input: Dict[str, Any]) -> str:
    """Proxy к mcp_knowledge.search()."""
    query = input.get("query", "")
    limit = min(input.get("limit", 5), 10)
    source_type = input.get("source_type")

    try:
        results = await mcp_knowledge.search(
            query=query,
            limit=limit,
            source_type=source_type,
        )

        if not results:
            return json.dumps({"results": [], "message": "No results found"}, ensure_ascii=False)

        # Форматируем результаты компактно для Claude
        formatted = []
        for item in results:
            if isinstance(item, dict):
                formatted.append({
                    "text": (item.get("text", item.get("content", "")))[:2000],
                    "source": item.get("source", ""),
                    "source_type": item.get("source_type", "pack"),
                    "score": item.get("score", 0),
                })
            elif isinstance(item, str):
                formatted.append({"text": item[:2000]})

        logger.info(f"search_knowledge: {len(formatted)} results for '{query[:50]}'")
        return json.dumps({"results": formatted}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"search_knowledge error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _exec_search_guides(input: Dict[str, Any]) -> str:
    """Proxy к mcp_knowledge.search(source_type='guides')."""
    query = input.get("query", "")
    limit = min(input.get("limit", 5), 10)

    try:
        results = await mcp_knowledge.search(
            query=query,
            limit=limit,
            source_type="guides",
        )

        if not results:
            return json.dumps({"results": [], "message": "No guides found"}, ensure_ascii=False)

        formatted = []
        for item in results:
            if isinstance(item, dict):
                formatted.append({
                    "text": (item.get("text", item.get("content", "")))[:2000],
                    "source": item.get("source", ""),
                    "score": item.get("score", 0),
                })
            elif isinstance(item, str):
                formatted.append({"text": item[:2000]})

        logger.info(f"search_guides: {len(formatted)} results for '{query[:50]}'")
        return json.dumps({"results": formatted}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"search_guides error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _exec_read_digital_twin(
    input: Dict[str, Any],
    telegram_user_id: Optional[int] = None,
) -> str:
    """Proxy к digital_twin.read()."""
    path = input.get("path", "")

    if not telegram_user_id:
        return json.dumps({"error": "User not connected to Digital Twin"}, ensure_ascii=False)

    if not digital_twin.is_connected(telegram_user_id):
        return json.dumps({"error": "User not authorized in Digital Twin"}, ensure_ascii=False)

    try:
        data = await digital_twin.read(path, telegram_user_id)

        if data is None:
            return json.dumps({"data": None, "message": f"No data at path '{path}'"}, ensure_ascii=False)

        logger.info(f"read_digital_twin: path='{path}', user={telegram_user_id}")
        return json.dumps({"data": data}, ensure_ascii=False, default=str)

    except Exception as e:
        logger.error(f"read_digital_twin error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# =============================================================================
# STANDARD CLAUDE.MD LOADER
# =============================================================================

_standard_claude_md_cache: Optional[str] = None


def get_standard_claude_md() -> str:
    """Загружает standard CLAUDE.md для T2+ system prompt.

    Содержит ключевые методологические принципы (FPF, различения, протоколы),
    доступные всем пользователям с ЦД (T2+).

    Путь: config/standard_claude.md (хранится в боте, обновляется template-sync).
    """
    global _standard_claude_md_cache

    if _standard_claude_md_cache is not None:
        return _standard_claude_md_cache

    from pathlib import Path
    path = Path(__file__).parent.parent.parent / "config" / "standard_claude.md"

    if path.exists():
        try:
            _standard_claude_md_cache = path.read_text(encoding="utf-8")
            logger.info(f"Standard CLAUDE.md loaded: {len(_standard_claude_md_cache)} chars")
            return _standard_claude_md_cache
        except Exception as e:
            logger.warning(f"Failed to read standard CLAUDE.md: {e}")

    # Fallback: пустая строка (T1 поведение)
    _standard_claude_md_cache = ""
    logger.info("Standard CLAUDE.md: not found, T1 mode")
    return _standard_claude_md_cache


def invalidate_standard_claude_cache():
    """Сброс кеша standard CLAUDE.md (для hot reload)."""
    global _standard_claude_md_cache
    _standard_claude_md_cache = None


# =============================================================================
# PERSONAL CLAUDE.MD LOADER (T3)
# =============================================================================

# In-memory кеш: telegram_user_id -> (content, timestamp)
_personal_claude_cache: Dict[int, tuple] = {}
_PERSONAL_CACHE_TTL = 300  # 5 минут


async def get_personal_claude_md(telegram_user_id: int) -> str:
    """Загружает personal CLAUDE.md из GitHub (T3 Со-мыслитель).

    Читает exocortex/CLAUDE.md из strategy_repo пользователя.
    Содержит персональную методологию, правила и контекст.

    Кешируется на 5 минут (GitHub API rate limits).

    Returns:
        Содержимое personal CLAUDE.md или пустая строка.
    """
    import time

    # Проверяем кеш
    cached = _personal_claude_cache.get(telegram_user_id)
    if cached:
        content, ts = cached
        if time.time() - ts < _PERSONAL_CACHE_TTL:
            return content

    from clients.github_oauth import github_oauth
    from clients.github_strategy import github_strategy

    strategy_repo = await github_oauth.get_strategy_repo(telegram_user_id)
    if not strategy_repo:
        logger.info(f"Personal CLAUDE.md: no strategy_repo for user {telegram_user_id}")
        _personal_claude_cache[telegram_user_id] = ("", time.time())
        return ""

    # Читаем exocortex/CLAUDE.md
    claude_md = await github_strategy.read_file(
        telegram_user_id, strategy_repo, "exocortex/CLAUDE.md"
    )

    if not claude_md:
        logger.info(f"Personal CLAUDE.md: not found in {strategy_repo}/exocortex/")
        _personal_claude_cache[telegram_user_id] = ("", time.time())
        return ""

    # Ограничиваем размер (system prompt budget)
    MAX_PERSONAL_CHARS = 6000
    if len(claude_md) > MAX_PERSONAL_CHARS:
        claude_md = claude_md[:MAX_PERSONAL_CHARS] + "\n\n[...усечено...]"

    logger.info(
        f"Personal CLAUDE.md loaded: {len(claude_md)} chars "
        f"from {strategy_repo} for user {telegram_user_id}"
    )
    _personal_claude_cache[telegram_user_id] = (claude_md, time.time())
    return claude_md


def invalidate_personal_claude_cache(telegram_user_id: Optional[int] = None):
    """Сброс кеша personal CLAUDE.md."""
    if telegram_user_id:
        _personal_claude_cache.pop(telegram_user_id, None)
    else:
        _personal_claude_cache.clear()
